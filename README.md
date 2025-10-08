# Enuygun Flight Search Automation

Automates the Enuygun.com flight search workflow and performs descriptive analytics over the collected results.

## Prerequisites
- Python 3.9+
- Installed packages: `selenium`, `pandas`, `matplotlib`, `seaborn`
- Google Chrome or Chromium browser + matching ChromeDriver in `PATH`

Install Python dependencies:
```bash
pip install selenium pandas matplotlib seaborn
```

## Usage
Run the scraper (headless mode is optional and can be enabled with `--headless`):
```bash
python analysis/flight_scraper.py --origin "İstanbul" --destination "Lefkoşa" --departure-date 2024-06-01 --headless
```
If the on-page form layout changes, the scraper automatically falls back to a direct results URL. You can steer the fallback explicitly by supplying slugs:
```bash
python analysis/flight_scraper.py --origin "İstanbul" --destination "Lefkoşa" --origin-slug istanbul --destination-slug ercan --departure-date 2024-06-01 --headless
```

Run the analysis once `analysis/flight_data.csv` exists:
```bash
python analysis/flight_analysis.py
```

The analysis command prints price statistics, highlights cost-effective flights, and saves charts into `analysis/reports/`:
- `price_by_airline.png` visualises the average price per airline.
- `price_heatmap.png` visualises price distribution across departure time slots.
