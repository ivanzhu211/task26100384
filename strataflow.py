"""StrataFlow dependency-edge incremental resolution library.

Nodes are identified by (hostname, primary_ip). Each node carries a full
address set (including primary_ip) and a full set of outbound connection
declarations (target_ip, protocol, port), deduplicated per source node.

Resolution rule: a declaration's candidate targets are all live nodes whose
address set contains target_ip. Exactly one candidate yields an edge; zero
or multiple candidates yield none, but the declaration is kept. Edges are
deduplicated by (src identity, dst identity, protocol, port); self-loops
are allowed.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional, Set, Tuple

NodeId = Tuple[str, str]                # (hostname, primary_ip)
Declaration = Tuple[str, str, int]      # (target_ip, protocol, port)
Edge = Tuple[NodeId, NodeId, str, int]  # (src, dst, protocol, port)


class Node:
    """A node upsert payload: identity plus full address/declaration sets."""

    def __init__(
        self,
        hostname: str,
        primary_ip: str,
        addresses: Optional[Iterable[str]] = None,
        declarations: Optional[Iterable[Declaration]] = None,
    ) -> None:
        self.hostname = hostname
        self.primary_ip = primary_ip
        self.addresses: Set[str] = set(addresses or ()) | {primary_ip}
        self.declarations: Set[Declaration] = set(declarations or ())

    @property
    def node_id(self) -> NodeId:
        return (self.hostname, self.primary_ip)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    hostname   TEXT NOT NULL,
    primary_ip TEXT NOT NULL,
    PRIMARY KEY (hostname, primary_ip)
);
CREATE TABLE IF NOT EXISTS node_addresses (
    hostname   TEXT NOT NULL,
    primary_ip TEXT NOT NULL,
    address    TEXT NOT NULL,
    PRIMARY KEY (hostname, primary_ip, address),
    FOREIGN KEY (hostname, primary_ip)
        REFERENCES nodes (hostname, primary_ip) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS declarations (
    src_hostname   TEXT NOT NULL,
    src_primary_ip TEXT NOT NULL,
    target_ip      TEXT NOT NULL,
    protocol       TEXT NOT NULL,
    port           INTEGER NOT NULL,
    PRIMARY KEY (src_hostname, src_primary_ip, target_ip, protocol, port),
    FOREIGN KEY (src_hostname, src_primary_ip)
        REFERENCES nodes (hostname, primary_ip) ON DELETE CASCADE
);
"""

# A declaration resolves to an edge iff exactly one live node owns target_ip.
_RESOLVE_SQL = """
SELECT d.src_hostname, d.src_primary_ip,
       n.hostname, n.primary_ip,
       d.protocol, d.port
FROM declarations d
JOIN node_addresses a ON a.address = d.target_ip
JOIN nodes n ON n.hostname = a.hostname AND n.primary_ip = a.primary_ip
GROUP BY d.src_hostname, d.src_primary_ip, d.target_ip, d.protocol, d.port
HAVING COUNT(*) = 1
"""


class StrataFlow:
    """Incremental dependency-edge resolver backed by SQLite."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StrataFlow":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def _resolve(self) -> Set[Edge]:
        """Full reparse of all live nodes and their stored declarations."""
        edges: Set[Edge] = set()
        for sh, si, dh, di, proto, port in self._conn.execute(_RESOLVE_SQL):
            edges.add(((sh, si), (dh, di), proto, port))
        return edges

    def apply_batch(
        self,
        upserts: Iterable[Node] = (),
        deletes: Iterable[NodeId] = (),
    ) -> Tuple[Set[Edge], Set[Edge]]:
        """Atomically apply a batch; return (added_edges, removed_edges).

        Upserts fully replace a node's addresses and declarations; deletes
        remove the node and its own outbound declarations only. The returned
        sets are the net edge diff between before and after the batch.
        """
        upserts = list(upserts)
        deletes = list(deletes)
        self._validate(upserts, deletes)
        before = self._resolve()
        try:
            self._conn.execute("BEGIN")
            for node in upserts:
                nid = node.node_id
                self._conn.execute(
                    "INSERT OR REPLACE INTO nodes (hostname, primary_ip)"
                    " VALUES (?, ?)",
                    nid,
                )
                self._conn.execute(
                    "DELETE FROM node_addresses WHERE hostname = ? AND primary_ip = ?",
                    nid,
                )
                self._conn.execute(
                    "DELETE FROM declarations WHERE src_hostname = ? AND src_primary_ip = ?",
                    nid,
                )
                self._conn.executemany(
                    "INSERT INTO node_addresses (hostname, primary_ip, address)"
                    " VALUES (?, ?, ?)",
                    [(nid[0], nid[1], addr) for addr in node.addresses],
                )
                self._conn.executemany(
                    "INSERT INTO declarations"
                    " (src_hostname, src_primary_ip, target_ip, protocol, port)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        (nid[0], nid[1], tip, proto, port)
                        for tip, proto, port in node.declarations
                    ],
                )
            for nid in deletes:
                self._conn.execute(
                    "DELETE FROM nodes WHERE hostname = ? AND primary_ip = ?",
                    nid,
                )
            after = self._resolve()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return after - before, before - after

    @staticmethod
    def _validate(upserts: List[Node], deletes: List[NodeId]) -> None:
        for node in upserts:
            for addr in node.addresses:
                if not isinstance(addr, str):
                    raise ValueError(f"invalid address: {addr!r}")
            for decl in node.declarations:
                tip, proto, port = decl
                if not isinstance(tip, str) or not isinstance(proto, str):
                    raise ValueError(f"invalid declaration: {decl!r}")
                if not isinstance(port, int) or isinstance(port, bool):
                    raise ValueError(f"invalid port in declaration: {decl!r}")
        for nid in deletes:
            if (
                not isinstance(nid, tuple)
                or len(nid) != 2
                or not all(isinstance(part, str) for part in nid)
            ):
                raise ValueError(f"invalid node id: {nid!r}")

    def read_graph(self) -> Set[Edge]:
        """Return the currently resolved edge set."""
        return self._resolve()
