"""ASCII DAG rendering for run progress. Copied verbatim from dbos-example.

Pure functions over (nodes, edges, statuses); no DB or DBOS coupling.
"""


def topological_sort(nodes, edges):
    adj = {n: [] for n in nodes}
    in_degree = {n: 0 for n in nodes}
    for u, v in edges:
        if u in adj and v in adj:
            adj[u].append(v)
            in_degree[v] += 1

    sources = [n for n in nodes if in_degree[n] == 0]
    sources.sort()

    order = []
    while sources:
        u = sources.pop(0)
        order.append(u)
        for v in adj[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                sources.append(v)
                sources.sort()

    if len(order) < len(nodes):
        remaining = [n for n in nodes if n not in order]
        remaining.sort()
        order.extend(remaining)
    return order


def render_ascii_graph(nodes, edges, node_statuses):
    order = topological_sort(nodes, edges)
    parents = {n: [] for n in nodes}
    children = {n: [] for n in nodes}
    for u, v in edges:
        parents[v].append(u)
        children[u].append(v)

    lines = []
    active_lanes = []

    for n in order:
        status = node_statuses.get(n, "pending")
        parent_indices = [i for i, lane in enumerate(active_lanes) if lane == n]

        if not parent_indices:
            node_idx = len(active_lanes)
            node_line_parts = ["|"] * node_idx + ["*"]
            node_line = " ".join(node_line_parts) + f"  {n} [{status}]"
            lines.append(node_line)

            targets = children[n]
            if targets:
                active_lanes.append(targets[0])
                if len(targets) > 1:
                    branch_parts = ["|"] * node_idx + ["|\\"] + ["\\"] * (len(targets) - 2)
                    lines.append(" ".join(branch_parts))
                    for t in targets[1:]:
                        active_lanes.append(t)
        else:
            p_idx = parent_indices[0]
            node_line_parts = []
            for i in range(len(active_lanes)):
                if i == p_idx:
                    node_line_parts.append("*")
                elif i in parent_indices:
                    node_line_parts.append("|")
                else:
                    node_line_parts.append("|")

            node_line = " ".join(node_line_parts) + f"  {n} [{status}]"
            lines.append(node_line)

            targets = children[n]
            if len(parent_indices) > 1:
                merge_parts = []
                for i in range(len(active_lanes)):
                    if i == p_idx:
                        merge_parts.append("|")
                    elif i in parent_indices:
                        merge_parts.append("/")
                    else:
                        merge_parts.append("|")
                lines.append(" ".join(merge_parts))

            new_lanes = []
            inserted_targets = False
            for i, lane in enumerate(active_lanes):
                if i == p_idx:
                    if targets:
                        new_lanes.extend(targets)
                        inserted_targets = True
                elif i in parent_indices:
                    pass
                else:
                    new_lanes.append(lane)
            if not inserted_targets and targets:
                new_lanes.extend(targets)
            active_lanes = new_lanes

    return "\n".join(lines)
