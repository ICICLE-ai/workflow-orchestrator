from app.utils.graph import render_ascii_graph, topological_sort


def test_topological_sort():
    """Test that topological_sort correctly orders nodes based on their dependencies."""
    nodes = ["A", "B", "C", "D"]
    edges = [("A", "C"), ("B", "C"), ("C", "D")]
    order = topological_sort(nodes, edges)
    assert order.index("A") < order.index("C")
    assert order.index("B") < order.index("C")
    assert order.index("C") < order.index("D")


def test_render_ascii_graph_simple():
    """Test that render_ascii_graph produces the expected string representation of a DAG."""
    nodes = ["A", "B", "C"]
    edges = [("A", "B"), ("B", "C")]
    statuses = {"A": "completed", "B": "running", "C": "pending"}
    graph = render_ascii_graph(nodes, edges, statuses)
    expected = "*  A [completed]\n*  B [running]\n*  C [pending]"
    assert graph.strip() == expected.strip()


if __name__ == "__main__":
    print("Running unit tests...")
    test_topological_sort()
    print("test_topological_sort: PASS")
    test_render_ascii_graph_simple()
    print("test_render_ascii_graph_simple: PASS")
    print("All unit tests passed!")
