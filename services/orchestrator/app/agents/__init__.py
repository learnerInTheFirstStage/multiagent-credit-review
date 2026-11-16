"""Specialist agents, called as tools by the graph.

One folder per agent. Each folder exposes one entry function,
investigate(request) -> response, with the shapes from packages/contracts.
The graph imports only that function.
"""
