"""Coordinate collector: drive the simulator, capture GUI_TREE snapshots.

Usage: python collect.py <win_name> [win_param]
  e.g. python collect.py CALCULATOR 0
Prints parsed nodes (text -> center point) for the page.
"""
from __future__ import annotations

import json
import sys
import time

from sim_client import SimClient

WRAP = "srv_quick_cmd send TOP5STEP:{};"
PING_SEQ = [0]
ACTIVITY_SEQ = [10_000]


def wrap(raw: str) -> str:
    return WRAP.format(raw[1:])


class Sim:
    def __init__(self):
        self.client = SimClient()

    def start(self):
        self.client.start()
        time.sleep(2)

    def stop(self):
        self.client.stop()

    def send(self, raw: str, request: str, end_type: str = "command_result", timeout: float = 20.0):
        return self.client.send(wrap(raw), request, end_type, timeout=timeout)

    def enter_page(self, win: str, param: int = 0):
        self.send(f":ENTER_PAGE:{win},{param}", "enter_page")
        self.ping()

    def ping(self):
        PING_SEQ[0] += 1
        return self.send(f":GUI_PING:{PING_SEQ[0]}", "gui_ping", "gui_ack")

    def gui_tree(self, seq: int = 1):
        return self.send(f":GUI_TREE:{seq}", "gui_tree", "gui_tree_end")

    def click(self, x: int, y: int, t: int = 1):
        self.send(f":TP_CLICK:{x},{y},{t}", "tp_click")
        self.ping()

    def swipe(self, d: int, t: int = 0):
        self.send(f":SWIPE_SIM:{d},{t}", "swipe_sim")
        self.ping()

    def activity(
        self,
        steps: int = 0,
        calories: int = 0,
        distance: int = 0,
        profile: str = "fixed",
    ):
        """Set the Windows simulator's persistent activity snapshot."""
        ACTIVITY_SEQ[0] += 1
        self.send(
            f":SIM_ACTIVITY_SET:{ACTIVITY_SEQ[0]},{profile},{steps},{calories},{distance}",
            "sim_activity_set",
        )


def collect_nodes(events: list[dict]) -> list[dict]:
    nodes = []
    for e in events:
        if e.get("type") == "gui_tree_node":
            nodes.append(e)
    return nodes


def center(node: dict) -> tuple[int, int]:
    r = node["rect"]
    return (r["x"] + r["w"] // 2, r["y"] + r["h"] // 2)


def text_map(nodes: list[dict]) -> dict[str, list[dict]]:
    m: dict[str, list[dict]] = {}
    for n in nodes:
        t = n.get("text")
        if t:
            m.setdefault(t, []).append(n)
    return m


if __name__ == "__main__":
    win = sys.argv[1] if len(sys.argv) > 1 else "CALCULATOR"
    param = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    sim = Sim()
    sim.start()
    try:
        sim.enter_page(win, param)
        events = sim.gui_tree()
        nodes = collect_nodes(events)
        print(f"== {win} nodes: {len(nodes)}")
        tm = text_map(nodes)
        for text in sorted(tm):
            for n in tm[text]:
                cx, cy = center(n)
                print(f"  text={text!r} rect={n['rect']} center=({cx},{cy}) clickable={n.get('clickable')} type={n.get('widget_type')}")
    finally:
        sim.stop()
