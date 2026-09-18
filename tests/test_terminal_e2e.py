"""通过真实 PTY 和本地 SSE 服务测试 Arc CLI 终端交互。"""

import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def text_response(text: str) -> list[bytes]:
    """创建一次成功的流式文本响应。"""

    return [
        _data({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}),
        _data(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
            }
        ),
        b"data: [DONE]\n\n",
    ]


def fragmented_text_response(fragments: list[str]) -> list[bytes]:
    """创建由许多细小增量组成的文本响应。"""

    packets = [
        _data({"choices": [{"index": 0, "delta": {"content": fragment}, "finish_reason": None}]})
        for fragment in fragments
    ]
    packets.extend(
        [
            _data(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    return packets


def tool_response(name: str, arguments: dict[str, object]) -> list[bytes]:
    """创建一次包含完整 function call 的流式响应。"""

    call = {
        "index": 0,
        "id": "pty-call",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return [
        _data({"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]}),
        _data(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
            }
        ),
        b"data: [DONE]\n\n",
    ]


def _data(payload: dict[str, object]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class MockModelServer(ThreadingHTTPServer):
    """按顺序返回预设 SSE 响应的本地模型服务。"""

    def __init__(self, responses: list[list[bytes]]):
        super().__init__(("127.0.0.1", 0), MockModelHandler)
        self.responses = responses
        self.requests: list[dict[str, object]] = []
        self.lock = threading.Lock()


class MockModelHandler(BaseHTTPRequestHandler):
    """处理测试中的 Chat Completions 请求。"""

    server: MockModelServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        with self.server.lock:
            self.server.requests.append(payload)
            response = self.server.responses.pop(0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # 保留短暂静默期，让真实终端有机会展示并刷新 thinking spinner。
        time.sleep(0.2)
        try:
            for packet in response:
                self.wfile.write(packet)
                self.wfile.flush()
                time.sleep(0.02)
        except BrokenPipeError:
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


class PtyProcess:
    """管理一个连接到真实伪终端的 Arc CLI 子进程。"""

    def __init__(self, cwd: Path, port: int, *, columns: int = 110):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, columns, 0, 0))
        env = os.environ.copy()
        env.update(
            {
                "ARC_API_KEY": "test-key",
                "ARC_BASE_URL": f"http://127.0.0.1:{port}/v1",
                "ARC_MODEL": "arc-pty-model",
                "NO_PROXY": "127.0.0.1,localhost",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "TERM": "xterm-256color",
            }
        )
        env.pop("NO_COLOR", None)
        self.master = master
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "arc_cli",
                "--cwd",
                str(cwd),
                "--no-session",
                "--thinking",
                "disabled",
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        self.output = bytearray()

    def send(self, text: str) -> None:
        os.write(self.master, text.encode())

    def wait_for(self, text: str, timeout: float = 6) -> str:
        """读取 PTY，直到出现目标文本或超时。"""

        target = text.encode()
        deadline = time.monotonic() + timeout
        while target not in self.output and time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.1)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 8192)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)
            # prompt_toolkit 通过 CPR 查询光标位置；模拟终端回应最后一行的位置。
            if b"\x1b[6n" in chunk:
                os.write(self.master, b"\x1b[32;1R")
        decoded = self.output.decode(errors="replace")
        if target not in self.output:
            raise AssertionError(f"Timed out waiting for {text!r}. Output:\n{decoded}")
        return decoded

    def close(self) -> None:
        """退出 Arc CLI，并确保测试子进程不会泄漏。"""

        if self.process.poll() is None:
            self.send("/quit\n")
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
        os.close(self.master)


class TerminalE2ETests(unittest.TestCase):
    def run_scenario(
        self,
        responses: list[list[bytes]],
        action,
        *,
        columns: int = 110,
    ) -> tuple[str, MockModelServer]:
        """在本地模型服务和真实 PTY 中运行一个交互场景。"""

        server = MockModelServer(responses)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as directory:
            terminal = PtyProcess(Path(directory), server.server_port, columns=columns)
            try:
                terminal.wait_for("/help commands")
                action(terminal)
                terminal.close()
                output = terminal.output.decode(errors="replace")
            finally:
                if terminal.process.poll() is None:
                    terminal.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
        return output, server

    def test_real_pty_banner_spinner_answer_and_status(self) -> None:
        def action(terminal: PtyProcess) -> None:
            terminal.send("hello\n")
            terminal.wait_for("PTY ready")
            terminal.wait_for("tokens")
            terminal.send("/status\n")
            terminal.wait_for("usage")
            terminal.wait_for("idle")

        output, server = self.run_scenario([text_response("PTY ready")], action)
        self.assertIn("ARC 0.1.2", output)
        self.assertIn("↳ steer", output)
        self.assertIn("arc-pty-model", output)
        self.assertIn("thinking", output)
        self.assertIn("PTY ready", output)
        self.assertNotIn("arc │", output)
        self.assertIn("tokens", output)
        self.assertIn("usage", output)
        self.assertIn("42 in", output)
        self.assertIn("state", output)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("max_completion_tokens", server.requests[0])

    def test_real_pty_fragmented_chinese_markdown_preserves_output_and_input(self) -> None:
        fragments = [
            "# 上",
            "海大学",
            "简介\n\n",
            "上海大学是一所综合",
            "性研究型大学。\n\n",
            "## 校区\n\n",
            "- 宝山校区\n",
            "- 延长校区\n",
            "- 嘉定校区\n\n",
            "**优势学科**：工程、材料与艺术。",
        ]

        def action(terminal: PtyProcess) -> None:
            terminal.send("介绍一下上海大学\n")
            terminal.wait_for("thinking")
            # 模型仍在输出时预输入命令，验证 prompt 重绘不会吞掉用户输入。
            terminal.send("/st")
            terminal.wait_for("工程、材料与艺术。")
            terminal.send("atus\n")
            terminal.wait_for("idle")

        output, server = self.run_scenario(
            [fragmented_text_response(fragments)],
            action,
            columns=48,
        )
        for expected in (
            "上海大学简介",
            "上海大学是一所综合性研究型大学。",
            "校区",
            "宝山校区",
            "延长校区",
            "嘉定校区",
            "工程、材料与艺术。",
        ):
            self.assertIn(expected, output)
        self.assertNotIn("# 上海大学简介", output)
        self.assertNotIn("**优势学科**", output)
        self.assertIn("state", output)
        self.assertIn("idle", output)
        question_end = output.find("介绍一下上海大学") + len("介绍一下上海大学")
        answer_end = output.find("工程、材料与艺术。") + len("工程、材料与艺术。")
        self.assertGreater(question_end, len("介绍一下上海大学"))
        self.assertGreater(answer_end, question_end)
        self.assertIn("↳ ", output[question_end:answer_end])
        self.assertIn("› ", output[answer_end:])
        self.assertEqual(len(server.requests), 1)

    def test_real_pty_unbroken_cjk_stream_and_prefilled_input_are_not_overwritten(self) -> None:
        expected = (
            "**流式验证**：中文片段不会被重绘覆盖，`inline_code` 保持完整，"
            "同时已经输入一半的终端命令也不会丢失。"
        )
        fragments = [expected[index : index + 2] for index in range(0, len(expected), 2)]

        def action(terminal: PtyProcess) -> None:
            terminal.send("stream one long line\n")
            terminal.wait_for("thinking")
            terminal.send("/sta")
            terminal.wait_for("终端命令也不会丢失。")
            terminal.wait_for("tokens · 42 in")
            terminal.send("tus\n")
            terminal.wait_for("idle")

        output, server = self.run_scenario([fragmented_text_response(fragments)], action, columns=44)
        self.assertIn("流式验证", output)
        self.assertIn("中文片段不会被重绘覆盖", output)
        self.assertIn("inline_code", output)
        self.assertIn("终端命令也不会丢失。", output)
        self.assertNotIn("**流式验证**", output)
        self.assertIn("/status", output)
        self.assertIn("state", output)
        self.assertIn("idle", output)
        self.assertEqual(len(server.requests), 1)

    def test_real_pty_tool_summary_and_last_tool(self) -> None:
        responses = [
            tool_response("bash", {"command": "sleep 0.25; printf 'pty-tool-ok\\n'"}),
            text_response("tool complete"),
        ]

        def action(terminal: PtyProcess) -> None:
            terminal.send("run a command\n")
            terminal.wait_for("tool complete")
            time.sleep(0.1)
            terminal.send("/last-tool\n")
            terminal.wait_for("/last-tool")
            terminal.wait_for("[Exit code: 0]")

        output, server = self.run_scenario(responses, action)
        # prompt_toolkit 使用增量光标更新，状态文本在原始 PTY 字节中可能被控制序列分段。
        self.assertIn("bash", output)
        self.assertIn("✓ bash", output)
        self.assertIn("pty-tool-ok", output)
        self.assertIn("tool complete", output)
        self.assertIn("/last-tool", output)
        self.assertEqual(len(server.requests), 2)

    def test_real_pty_failed_tool_history_and_tree(self) -> None:
        responses = [
            tool_response("bash", {"command": "printf 'expected-failure\\n'; exit 7"}),
            text_response("failure inspected"),
        ]

        def action(terminal: PtyProcess) -> None:
            terminal.send("run the failing command\n")
            terminal.wait_for("failure inspected")
            time.sleep(0.1)
            terminal.send("/history\n")
            terminal.wait_for("tool:bash error")
            terminal.send("/tree\n")
            terminal.wait_for("current")

        output, server = self.run_scenario(responses, action)
        self.assertIn("expected-failure", output)
        self.assertIn("✗ bash", output)
        self.assertIn("tool:bash error", output)
        self.assertIn("●", output)
        self.assertIn("current", output)
        self.assertEqual(len(server.requests), 2)

    def test_real_pty_ctrl_c_cancels_running_tool_and_returns_to_prompt(self) -> None:
        responses = [tool_response("bash", {"command": "sleep 5; printf 'slow-finished\\n'"})]

        def action(terminal: PtyProcess) -> None:
            terminal.send("start a slow command\n")
            terminal.wait_for("bash")
            terminal.send("\x03")
            terminal.wait_for("aborted")
            terminal.send("/status\n")
            terminal.wait_for("idle")

        output, server = self.run_scenario(responses, action)
        self.assertIn("aborted", output)
        self.assertIn("state", output)
        self.assertIn("idle", output)
        self.assertNotIn("✓ bash", output)
        self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
