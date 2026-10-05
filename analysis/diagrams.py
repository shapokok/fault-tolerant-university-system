"""Draws the FT architecture and the fault tree as PNG (matplotlib only, no graphviz needed).

Run from the project root: python3 analysis/diagrams.py
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

PLOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "plots")


def box(ax, x, y, text, w=1.9, h=0.6, color="#e8f1f8", edge="#2e86ab"):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.04",
                                fc=color, ec=edge, lw=1.3))
    ax.text(x, y, text, ha="center", va="center", fontsize=8.5)


def arrow(ax, a, b, text="", style="-|>", color="#333", ls="-"):
    ax.annotate("", xy=b, xytext=a, arrowprops=dict(arrowstyle=style, color=color, lw=1.1, ls=ls))
    if text:
        ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.12, text, fontsize=7, ha="center", color=color)


def architecture():
    fig, ax = plt.subplots(figsize=(12, 7.5))
    ax.set(xlim=(0, 12), ylim=(0, 8.2))
    ax.axis("off")

    # node regions
    ax.add_patch(FancyBboxPatch((0.3, 0.3), 5.6, 5.0, boxstyle="round,pad=0.05", fc="#f4f9f4", ec="#5a9a5a", ls="--"))
    ax.text(0.5, 5.05, "node-a", fontsize=10, color="#5a9a5a", weight="bold")
    ax.add_patch(FancyBboxPatch((6.2, 0.3), 3.6, 5.0, boxstyle="round,pad=0.05", fc="#fdf6ec", ec="#c98a2b", ls="--"))
    ax.text(8.7, 5.05, "node-b", fontsize=10, color="#c98a2b", weight="bold")

    box(ax, 6, 7.6, "Client / workload / Locust", w=3.0, color="#ffffff", edge="#555")
    box(ax, 6, 6.4, "gateway (nginx :8080)\nround robin, max_fails, next_upstream", w=4.2, h=0.75)
    arrow(ax, (6, 7.3), (6, 6.8))

    services = [("student-service", 4.2), ("transcript-service", 3.2), ("payment-service", 2.2)]
    for name, y in services:
        box(ax, 2.0, y, f"{name}-1")
        box(ax, 8.0, y, f"{name}-2")
        arrow(ax, (5.2, 6.0), (2.9, y + 0.2), color="#2e86ab")
        arrow(ax, (6.8, 6.0), (7.1, y + 0.2), color="#2e86ab")

    box(ax, 4.0, 1.0, "postgres-primary\n(read + write)", w=2.2, h=0.7, color="#eef4e6", edge="#5a9a5a")
    box(ax, 8.0, 1.0, "postgres-replica\n(read only)", w=2.2, h=0.7, color="#fbeedd", edge="#c98a2b")
    arrow(ax, (5.1, 1.0), (6.9, 1.0), "streaming replication", color="#5a9a5a")
    arrow(ax, (2.0, 1.9), (3.4, 1.35), color="#777")
    arrow(ax, (8.0, 1.9), (4.9, 1.35), color="#777")
    arrow(ax, (8.0, 1.9), (8.0, 1.35), color="#c98a2b", ls="--")
    ax.text(8.15, 1.55, "fallback reads\n(every service)", fontsize=7, color="#c98a2b")
    ax.text(2.2, 1.55, "every service\nuses the DB", fontsize=7, color="#777")

    box(ax, 11.0, 2.2, "mock-bank\n(external)", w=1.6, h=0.7, color="#f9e3e6", edge="#d1495b")
    arrow(ax, (8.95, 2.2), (10.2, 2.2), "charge\n(timeout, retry,\nbreaker)", color="#d1495b")
    arrow(ax, (2.95, 3.2), (5.0, 6.0), color="#888", ls="--")
    ax.text(3.2, 4.85, "transcript -> student (via gateway, breaker)", fontsize=7, color="#888")

    box(ax, 1.4, 7.35, "monitor\npolls /health,/ready\nevery 0.5 s", w=2.2, h=0.8, color="#f2f2f2", edge="#555")
    box(ax, 1.4, 6.2, "backup\npg_dump every 60 s", w=2.0, h=0.6, color="#f2f2f2", edge="#555")
    ax.text(10.1, 4.6, "Docker: restart: unless-stopped,\nhealthcheck every 2 s,\ninit: true",
            fontsize=8, color="#555", va="top")
    ax.set_title("Fault-tolerant architecture (docker-compose.ft.yml)", fontsize=12)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "architecture.png"), dpi=130)
    plt.close(fig)


def gate(ax, x, y, kind):
    color = {"OR": "#d1495b", "AND": "#2e86ab"}[kind]
    ax.add_patch(FancyBboxPatch((x - 0.35, y - 0.2), 0.7, 0.4, boxstyle="round,pad=0.02", fc=color, ec=color))
    ax.text(x, y, kind, ha="center", va="center", fontsize=8, color="white", weight="bold")


def link(ax, a, b):
    ax.plot([a[0], a[0], b[0], b[0]], [a[1], (a[1] + b[1]) / 2, (a[1] + b[1]) / 2, b[1]], color="#555", lw=1)


def fault_tree():
    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.set(xlim=(0, 13), ylim=(0, 7))
    ax.axis("off")

    box(ax, 6.5, 6.5, "TOP: user request fails (FT system)", w=4.0, color="#f9e3e6", edge="#d1495b")
    gate(ax, 6.5, 5.8, "OR")
    link(ax, (6.5, 6.2), (6.5, 6.0))

    mids = [
        (1.3, "Gateway down\n(single nginx,\nnot redundant)", None),
        (3.8, "All student-service\ninstances down", "AND"),
        (6.5, "All payment-service\ninstances down", "AND"),
        (9.0, "DB reads impossible", "AND"),
        (11.7, "Payment write fails", "OR"),
    ]
    leaves = {
        3.8: ["student-1 down\n(crash / node-a)", "student-2 down\n(crash / node-b)"],
        6.5: ["payment-1 down\n(crash / node-a)", "payment-2 down\n(crash / node-b)"],
        9.0: ["primary down", "replica down\n(node-b)"],
        11.7: ["primary down,\nno failover yet", "bank error /\nbreaker open"],
    }
    for x, text, kind in mids:
        link(ax, (6.5, 5.6), (x, 4.95))
        box(ax, x, 4.6, text, w=2.2, h=0.75, color="#fff7e0" if kind else "#e6e6e6", edge="#c98a2b" if kind else "#555")
        if kind:
            gate(ax, x, 3.8, kind)
            link(ax, (x, 4.22), (x, 4.0))
            for i, leaf in enumerate(leaves[x]):
                lx = x + (i - 0.5) * 1.25
                link(ax, (x, 3.6), (lx, 3.0))
                ax.add_patch(plt.Circle((lx, 2.55), 0.5, fc="#e8f1f8", ec="#2e86ab"))
                ax.text(lx, 2.55, leaf, ha="center", va="center", fontsize=6.6)

    ax.text(0.3, 1.2,
            "Masking (why many basic events do NOT reach the top):\n"
            "- AND gates = redundancy: one instance or the replica is enough.\n"
            "- Timetable from cache and partial transcripts hide DB / student-service failures for reads.\n"
            "- Unknown bank outcome returns 202 pending; reconciliation fixes the money later.\n"
            "Common cause: node-b failure hits student-2, transcript-2, payment-2 and the replica at once,\n"
            "so the AND gates are only safe while node-a is healthy.",
            fontsize=8.5, va="top")
    ax.set_title("Fault tree: user-visible failure in the FT system (circles = basic events)", fontsize=12)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "fault_tree.png"), dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs(PLOTS, exist_ok=True)
    architecture()
    fault_tree()
    print("wrote architecture.png and fault_tree.png")
