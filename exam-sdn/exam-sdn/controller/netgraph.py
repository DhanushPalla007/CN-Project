"""Switch-level topology graph: shortest paths and a loop-free flooding tree (no Ryu needed)."""
from collections import deque


class Graph:
    def __init__(self):
        self.port = {}      # (a, b) -> port on switch a that leads to switch b
        self._tree = None

    def has_links(self):
        return bool(self.port)

    def add_link(self, a, pa, b, pb):
        changed = self.port.get((a, b)) != pa or self.port.get((b, a)) != pb
        self.port[(a, b)], self.port[(b, a)] = pa, pb
        self._tree = None
        return changed

    def del_link(self, a, b):
        changed = (a, b) in self.port or (b, a) in self.port
        self.port.pop((a, b), None)
        self.port.pop((b, a), None)
        self._tree = None
        return changed

    def neighbors(self, a):
        return sorted(b for (x, b) in self.port if x == a)

    def link_ports(self, a):
        return {p for (x, _), p in self.port.items() if x == a}

    def is_link_port(self, a, p):
        return p in self.link_ports(a)

    def path(self, src, dst):
        """Fewest-hop path [src, ..., dst], or None if unreachable (deterministic tie-break)."""
        if src == dst:
            return [src]
        prev, q = {src: None}, deque([src])
        while q:
            u = q.popleft()
            for v in self.neighbors(u):
                if v not in prev:
                    prev[v] = u
                    q.append(v)
        if dst not in prev:
            return None
        out, n = [], dst
        while n is not None:
            out.append(n)
            n = prev[n]
        return out[::-1]

    def hops(self, path, last_port):
        """[(switch, out_port), ...] along the path; last switch uses the host-facing port."""
        return [(path[i], self.port[(path[i], path[i + 1])]) for i in range(len(path) - 1)] + [(path[-1], last_port)]

    def tree_ports(self, dpid):
        """Link ports of dpid that belong to the spanning tree (safe to flood on)."""
        if self._tree is None:
            self._tree = self._build_tree()
        return self._tree.get(dpid, set())

    def _build_tree(self):
        seen, tree = set(), {}
        for root in sorted({a for (a, _) in self.port}):
            if root in seen:
                continue
            seen.add(root)
            q = deque([root])
            while q:
                u = q.popleft()
                for v in self.neighbors(u):
                    if v not in seen:
                        seen.add(v)
                        tree.setdefault(u, set()).add(self.port[(u, v)])
                        tree.setdefault(v, set()).add(self.port[(v, u)])
                        q.append(v)
        return tree
