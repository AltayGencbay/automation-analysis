"""
Flight data analysis utilities.

Reads `analysis/flight_data.csv`, computes descriptive statistics per airline,
generates bar/heat maps, and prints the most cost-effective flight options.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, Optional

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns


DEFAULT_INPUT = Path("analysis") / "flight_data.csv"
DEFAULT_REPORT_DIR = Path("analysis") / "reports"


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse Enuygun flight search results.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Path to the scraped flight data CSV.")
    parser.add_argument(
        "--reports-dir",
        default=str(DEFAULT_REPORT_DIR),
        help="Directory to store generated visualisations (default: analysis/reports).",
    )
    parser.add_argument("--top-n", type=int, default=5, help="Number of cost-effective flights to display.")
    return parser.parse_args(argv)


def load_data(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(f"Flight data CSV not found at {csv_path}")

    df = pd.read_csv(csv_path)
    if "price" not in df.columns:
        raise ValueError("CSV missing required `price` column.")

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["duration_minutes"] = pd.to_numeric(df.get("duration_minutes"), errors="coerce")
    df["airline"] = df["airline"].fillna("Unknown")

    df["departure_time"] = df["departure_time"].fillna("")
    df["arrival_time"] = df["arrival_time"].fillna("")
    return df.dropna(subset=["price"])


def compute_price_stats(df: pd.DataFrame) -> pd.DataFrame:
    stats = (
        df.groupby("airline", as_index=False)["price"]
        .agg(min_price="min", max_price="max", avg_price="mean")
        .sort_values(by="avg_price")
    )
    return stats


def ensure_reports_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def plot_average_price_bar(stats: pd.DataFrame, output_dir: Path) -> Path:
    plt.figure(figsize=(10, max(4, len(stats) * 0.5)))
    sns.barplot(data=stats, y="airline", x="avg_price", palette="viridis")
    plt.xlabel("Average Price (TRY)")
    plt.ylabel("Airline")
    plt.title("Average Flight Prices by Airline")
    plt.tight_layout()

    output_path = output_dir / "price_by_airline.png"
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def parse_time_to_minutes(time_str: str) -> Optional[int]:
    """Convert HH:MM strings into minutes since midnight."""
    if not isinstance(time_str, str) or ":" not in time_str:
        return None
    try:
        hours, minutes = time_str.strip().split(":")[:2]
        hours_int = int(hours)
        minutes_int = int(minutes)
        return hours_int * 60 + minutes_int
    except ValueError:
        return None


def assign_time_slot(minutes: Optional[int]) -> str:
    if minutes is None:
        return "Unknown"
    bins = [0, 240, 480, 720, 960, 1200, 1440]
    labels = [
        "00:00-03:59",
        "04:00-07:59",
        "08:00-11:59",
        "12:00-15:59",
        "16:00-19:59",
        "20:00-23:59",
    ]
    idx = np.digitize([minutes], bins, right=False)[0] - 1
    if 0 <= idx < len(labels):
        return labels[idx]
    return "Unknown"


def plot_heatmap(df: pd.DataFrame, output_dir: Path) -> Path:
    df = df.copy()
    df["departure_minutes"] = df["departure_time"].apply(parse_time_to_minutes)
    df["time_slot"] = df["departure_minutes"].apply(assign_time_slot)

    desired_order = [
        "00:00-03:59",
        "04:00-07:59",
        "08:00-11:59",
        "12:00-15:59",
        "16:00-19:59",
        "20:00-23:59",
        "Unknown",
    ]
    pivot = df.pivot_table(values="price", index="time_slot", columns="airline", aggfunc="mean")
    pivot = pivot.reindex(desired_order)

    plt.figure(figsize=(12, 6))
    sns.heatmap(
        pivot,
        annot=True,
        fmt=".0f",
        cmap="YlGnBu",
        cbar_kws={"label": "Average Price (TRY)"},
        linewidths=0.3,
        linecolor="#f0f0f0",
    )
    plt.xlabel("Airline")
    plt.ylabel("Departure Time Slot")
    plt.title("Average Price Heatmap by Departure Time Slot")
    plt.tight_layout()

    output_path = output_dir / "price_heatmap.png"
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def determine_cost_effective_flights(df: pd.DataFrame, top_n: int) -> pd.DataFrame:
    effective = df.copy()
    effective = effective[effective["duration_minutes"] > 0].copy()
    if effective.empty:
        return effective
    effective["price_per_minute"] = effective["price"] / effective["duration_minutes"]
    return effective.sort_values(by=["price_per_minute", "price"]).head(top_n)


def print_cost_effective_flights(flights: pd.DataFrame) -> None:
    if flights.empty:
        print("No flights with valid duration data found for cost-effectiveness analysis.")
        return

    columns = [
        "airline",
        "departure_time",
        "arrival_time",
        "price",
        "duration",
        "duration_minutes",
        "price_per_minute",
        "connection_info",
    ]
    display_cols = [col for col in columns if col in flights.columns]
    print("Most cost-effective flights (lowest price per minute):")
    print(flights[display_cols].to_string(index=False, formatters={"price": "{:.2f}".format, "price_per_minute": "{:.2f}".format}))


def main() -> None:
    args = parse_args()
    csv_path = Path(args.input)
    report_dir = ensure_reports_dir(Path(args.reports_dir))

    df = load_data(csv_path)
    price_stats = compute_price_stats(df)

    print("Price statistics by airline (TRY):")
    print(price_stats.to_string(index=False, formatters={"avg_price": "{:.2f}".format}))

    bar_chart_path = plot_average_price_bar(price_stats, report_dir)
    heatmap_path = plot_heatmap(df, report_dir)

    cost_effective = determine_cost_effective_flights(df, args.top_n)
    print_cost_effective_flights(cost_effective)

    print(f"Bar chart saved to {bar_chart_path}")
    print(f"Heatmap saved to {heatmap_path}")


if __name__ == "__main__":
    main()
