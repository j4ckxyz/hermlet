"""The smallest hermlet app: ask your agent something and print the answer.

    hermlet run &        # once, or `hermlet service install`
    python examples/hello.py
"""
import hermlet

with hermlet.connect() as h:
    print(h.ask("In one sentence: what can you help me with today?"))
