# task26100384

StrataFlow 依赖边增量解析库（Python3 标准库 + SQLite，无第三方依赖）。

## 接口说明

```python
from strataflow import StrataFlow, Node, NodeIdentity, Declaration, Edge

sf = StrataFlow("graph.db")          # 打开/创建 SQLite 数据库

node = Node(
    identity=NodeIdentity("web-1", "10.0.0.1"),   # 不可变身份 (hostname, primary_ip)
    addresses=frozenset({"10.0.0.1", "10.0.0.11"}),  # 完整地址集合，须含 primary_ip
    declarations=frozenset({Declaration("10.0.0.2", "tcp", 5432)}),  # 完整出向声明
)

added, removed = sf.apply_batch(upserts=[node], deletes=[NodeIdentity("old", "10.0.0.9")])
graph: set[Edge] = sf.read_graph()   # Edge(source, target, protocol, port)
sf.close()                           # 也支持 with 语句
```

- `apply_batch(upserts, deletes)`：upserts 完整替换节点地址与声明；deletes 删除节点及其出向声明（不影响其他节点指向这些地址的声明）。整个批次原子提交，返回批次前后边集合的净差 `(added_edges, removed_edges)`。
- 解析规则：声明的候选目标为地址集合包含 `target_ip` 的全部存活节点；恰有一个候选时生成边，零个或多个候选时只保存声明、不生成边，后续批次（含重开数据库后）会自动重新解析。
- 边身份为 `(源节点身份, 目标节点身份, protocol, port)`，允许自环；同一批次内短暂出现/消失的边不产生事件。

## 运行测试

```sh
python3 -m unittest test_strataflow -v
```
