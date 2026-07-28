"""Adapters — the layer that lets agents drive real DCC tools and engines.

Each adapter is a subprocess boundary: an agent hands over a script, the tool
runs headless, and the adapter returns structured facts (stats, renders, errors)
rather than raw log spew. The return value is the feedback loop — without it an
agent is writing bpy blind.
"""
