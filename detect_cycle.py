"""使用 Floyd 快慢指针算法检测单向链表中的环。

时间复杂度：O(n)，空间复杂度：O(1)
"""

from __future__ import annotations


class ListNode:
    """表示测试循环检测算法所用的单向链表节点。"""

    def __init__(self, val: int = 0, next_node: ListNode | None = None):
        """使用节点值和可选后继节点初始化链表节点。"""

        self.val = val
        self.next = next_node


def has_cycle(head: ListNode | None) -> bool:
    """当链表包含环时返回 ``True``，否则返回 ``False``。"""

    if not head or not head.next:
        return False

    slow = head
    fast = head.next

    while slow != fast:
        if not fast or not fast.next:
            return False
        slow = slow.next
        fast = fast.next.next

    return True


def test_has_cycle() -> None:
    """运行覆盖常见链表形态的基础断言。"""

    # 测试1：无环链表
    # 1 -> 2 -> 3 -> None
    node1 = ListNode(1)
    node2 = ListNode(2)
    node3 = ListNode(3)
    node1.next = node2
    node2.next = node3
    assert not has_cycle(node1), "测试1失败：无环链表应返回False"
    print("✓ 测试1通过：无环链表")

    # 测试2：有环链表（环在末尾）
    # 1 -> 2 -> 3 -> 2（形成环）
    node1 = ListNode(1)
    node2 = ListNode(2)
    node3 = ListNode(3)
    node1.next = node2
    node2.next = node3
    node3.next = node2  # 形成环
    assert has_cycle(node1), "测试2失败：有环链表应返回True"
    print("✓ 测试2通过：有环链表")

    # 测试3：单节点无环
    node1 = ListNode(1)
    assert not has_cycle(node1), "测试3失败：单节点无环应返回False"
    print("✓ 测试3通过：单节点无环")

    # 测试4：空链表
    assert not has_cycle(None), "测试4失败：空链表应返回False"
    print("✓ 测试4通过：空链表")

    # 测试5：环在头部
    # 1 -> 2 -> 3 -> 1（形成环）
    node1 = ListNode(1)
    node2 = ListNode(2)
    node3 = ListNode(3)
    node1.next = node2
    node2.next = node3
    node3.next = node1  # 形成环
    assert has_cycle(node1), "测试5失败：环在头部应返回True"
    print("✓ 测试5通过：环在头部")

    print("\n所有测试通过！")


def create_test_list(values: list[int], pos: int = -1) -> ListNode | None:
    """从值列表构建链表，并可将尾节点连接到指定位置。"""

    if not values:
        return None

    nodes = [ListNode(val) for val in values]
    for i in range(len(nodes) - 1):
        nodes[i].next = nodes[i + 1]

    if pos != -1:
        nodes[-1].next = nodes[pos]

    return nodes[0]


if __name__ == "__main__":
    print("链表循环检测算法测试\n")

    # 运行基本测试
    test_has_cycle()

    # 额外演示
    print("\n--- 额外演示 ---")

    # 创建有环链表：1 -> 2 -> 3 -> 4 -> 2
    head = create_test_list([1, 2, 3, 4], pos=1)
    result = has_cycle(head)
    print(f"链表 [1, 2, 3, 4] 连接到位置1: {result}")

    # 创建无环链表：1 -> 2 -> 3 -> 4
    head = create_test_list([1, 2, 3, 4], pos=-1)
    result = has_cycle(head)
    print(f"链表 [1, 2, 3, 4] 无环: {result}")
