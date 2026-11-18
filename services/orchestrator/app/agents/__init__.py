"""Specialist agents, called as tools by the graph.

One folder per agent. Each folder exposes one entry function,
one function from an investigate request to an investigate response
(cash_flow, card_service), with the shapes from packages/contracts.
The graph imports only that function.
"""
