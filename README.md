# StrataFlow 依赖边增量解析库

纯 Python3 标准库 + SQLite 实现，直接通过函数调用，无任何外部服务依赖。

## 接口说明

```python
from strataflow import Node, StrataFlow

sf = StrataFlow("graph.db")   # 传 ":memory:" 则不落盘；默认 ":memory:"

# 节点：身份为 (hostname, primary_ip)，不可变。
# addresses 为完整地址集合（primary_ip 自动并入）；declarations 为完整出向
# 声明集合，元素为 (target_ip, protocol, port)，同一源节点内自动去重。
node = Node("web-1", "10.0.0.1",
            addresses={"10.0.0.1", "10.0.0.2"},
            declarations={("10.0.0.9", "tcp", 443)})

# ApplyBatch：upserts 完整替换对应节点的地址与声明；deletes 删除节点及
# 其出向声明（不影响其他节点指向这些地址的声明）。整批原子提交。
# 返回 (added_edges, removed_edges)，为批次前后边集合的净差。
added, removed = sf.apply_batch(upserts=[node], deletes=[("old", "10.0.0.5")])

# ReadGraph：当前解析出的全部边。
# 边身份：((src_host, src_ip), (dst_host, dst_ip), protocol, port)，允许自环。
edges = sf.read_graph()

sf.close()
```

## 解析规则

- 一条声明的候选目标 = 地址集合包含 `target_ip` 的全部存活节点。
- 恰有一个候选时生成边；零个（未命中）或多个（歧义）时不生成边，但声明保留，
  后续批次（包括数据库重开后）地址归属变化会自动重新解析。
- 不同 `target_ip` 的声明可能解析出同一条边，图中只保留一条；只要仍有任一
  声明支撑，撤销其他声明不会产生删边事件。
- 任意批次提交后，图恰好等于对全部存活节点及其保存声明的全量解析结果；
  返回的变更集合是批次前后的净差，批次内短暂出现/消失的边不产生事件。

## 运行测试

```bash
python3 -m unittest -v
```
