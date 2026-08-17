"""Service-layer modules: business logic that spans more than one route handler.

A "service" here is a plain Python module (not a class hierarchy, not a
framework concept) that owns one piece of domain logic end-to-end so
blueprints stay thin — a route handler parses the request, calls a
service function, and turns the result into a JSON response; it should
rarely contain business rules itself. ``stock.py`` is the first and most
important example (see its module docstring): every inventory-changing
code path in the whole app funnels through it.
"""
