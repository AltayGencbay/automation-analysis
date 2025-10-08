"""
Flight search automation for Enuygun.com.

This script opens the flight search page, performs a search for the provided
route/date, extracts flight cards from the results, and stores them in
`analysis/flight_data.csv`. The implementation relies on Selenium with explicit
waits and attempts to be resilient against minor DOM changes by using multiple
selector fallbacks.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_URL = "https://www.enuygun.com/ucak-bileti/"
DEFAULT_OUTPUT = Path("analysis") / "flight_data.csv"


@dataclass
class FlightRecord:
    departure_time: str
    arrival_time: str
    airline: str
    price: float
    price_display: str
    connection_info: str
    duration: str
    duration_minutes: Optional[int]

    def as_dict(self) -> Dict[str, object]:
        return {
            "departure_time": self.departure_time,
            "arrival_time": self.arrival_time,
            "airline": self.airline,
            "price": self.price,
            "price_display": self.price_display,
            "connection_info": self.connection_info,
            "duration": self.duration,
            "duration_minutes": self.duration_minutes,
        }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract flight search results from Enuygun.com into CSV."
    )
    parser.add_argument("--origin", default="İstanbul", help="Origin city or airport name (default: İstanbul)")
    parser.add_argument("--destination", default="Lefkoşa", help="Destination city or airport name (default: Lefkoşa)")
    parser.add_argument(
        "--origin-slug",
        default=None,
        help="Optional slug override for origin when falling back to direct URL navigation.",
    )
    parser.add_argument(
        "--destination-slug",
        default=None,
        help="Optional slug override for destination when falling back to direct URL navigation.",
    )
    parser.add_argument(
        "--departure-date",
        dest="departure_date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Departure date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--return-date",
        dest="return_date",
        default=None,
        help="Return date in YYYY-MM-DD format (optional)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output CSV file path (default: analysis/flight_data.csv)",
    )
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--max-wait", type=int, default=45, help="Maximum wait time for dynamic elements (seconds)")
    return parser.parse_args(argv)


def configure_driver(headless: bool) -> webdriver.Chrome:
    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1600,1200")
    prefs = {"intl.accept_languages": "tr-TR,tr"}
    options.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


def accept_cookies(driver: webdriver.Chrome, timeout: int) -> None:
    possible_selectors = [
        (By.CSS_SELECTOR, "button[data-testid*='cookie'][data-testid*='accept']"),
        (By.CSS_SELECTOR, "button[id*='onetrust-accept']"),
        (By.CSS_SELECTOR, "button[class*='cookie'][class*='accept']"),
    ]

    def try_click_in_context() -> bool:
        for by_, selector in possible_selectors:
            try:
                button = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((by_, selector))
                )
                button.click()
                return True
            except TimeoutException:
                continue
            except Exception:
                continue
        return False

    # Direct attempt
    if try_click_in_context():
        return

    # Look into cookie consent iframes (OneTrust / SourcePoint patterns)
    iframe_selectors = [
        "iframe[id*='sp_message_iframe']",
        "iframe[src*='cookielaw']",
        "iframe[id*='ot-consent']",
        "iframe[src*='onetrust']",
    ]
    for iframe_sel in iframe_selectors:
        try:
            iframe = WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, iframe_sel))
            )
        except TimeoutException:
            continue

        try:
            driver.switch_to.frame(iframe)
            if try_click_in_context():
                driver.switch_to.default_content()
                return
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    # Fallback via XPath on button text
    try:
        xpath_button = WebDriverWait(driver, timeout // 2).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(translate(normalize-space(.),"
                    " 'ABCDEFGHIJKLMNOPQRSTUVWXYZĞİÖŞÜÇ',"
                    " 'abcdefghijklmnopqrstuvwxyzğıöşüç'), 'kabul')]",
                )
            )
        )
        xpath_button.click()
        return
    except TimeoutException:
        pass
    except Exception:
        pass

    # Fallback: attempt to use OneTrust default button via JavaScript
    try:
        driver.execute_script(
            """
            const button = document.querySelector('button#onetrust-accept-btn-handler');
            if (button) { button.click(); return true; }
            const otButtons = document.querySelectorAll('button');
            for (const btn of otButtons) {
                if (btn.textContent.trim().toLowerCase().includes('kabul')) {
                    btn.click();
                    return true;
                }
            }
            return false;
            """
        )
    except Exception:
        pass


def fill_route_inputs(
    driver: webdriver.Chrome,
    origin: str,
    destination: str,
    timeout: int,
) -> None:
    set_location(driver, origin, timeout, role="origin")
    set_location(driver, destination, timeout, role="destination")


def set_location(driver: webdriver.Chrome, value: str, timeout: int, role: str) -> None:
    search_keywords = {
        "origin": ["nereden", "origin", "kalkış", "from"],
        "destination": ["nereye", "destination", "varış", "to"],
    }

    keywords = search_keywords.get(role, [])
    target_input: Optional[WebElement] = None

    # Attempt to locate dedicated input via data-testid shorthand
    preferred_selectors = [
        f"input[data-testid*='{role}']",
        f"input[id*='{role}']",
        f"input[name*='{role}']",
        f"[data-testid*='{role}'] input",
    ]
    for selector in preferred_selectors:
        try:
            target_input = WebDriverWait(driver, timeout // 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            break
        except TimeoutException:
            continue

    # Fallback: fuzzy search against placeholders and aria labels
    if target_input is None:
        all_inputs = driver.find_elements(By.CSS_SELECTOR, "input")
        lower_value = [kw.lower() for kw in keywords]
        for element in all_inputs:
            placeholder = (element.get_attribute("placeholder") or "").lower()
            aria = (element.get_attribute("aria-label") or "").lower()
            data_testid = (element.get_attribute("data-testid") or "").lower()
            if any(kw in placeholder for kw in lower_value) or any(kw in aria for kw in lower_value) \
                    or any(kw in data_testid for kw in lower_value):
                target_input = element
                break

    if target_input is None:
        raise RuntimeError(f"Unable to locate {role} input on the search form.")

    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_input)
    target_input.click()
    target_input.send_keys(Keys.CONTROL, "a")
    target_input.send_keys(Keys.DELETE)
    target_input.send_keys(value)

    # Wait for suggestion dropdown and select the first matching result
    try:
        suggestion = WebDriverWait(driver, timeout // 2).until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "li[data-testid*='suggestion'], li[role='option'], ul[role='listbox'] li",
                )
            )
        )
        suggestion.click()
    except TimeoutException:
        target_input.send_keys(Keys.RETURN)


def set_dates(
    driver: webdriver.Chrome,
    departure_date: str,
    return_date: Optional[str],
    timeout: int,
) -> None:
    date_selectors = [
        "[data-testid*='departure-date']",
        "[data-testid*='datepicker-trigger']",
        "[class*='datepicker'] button",
        "button[id*='departure']",
        "button[data-testid*='origin-date']",
        "button[data-testid*='flight-date']",
        "[data-testid*='date-input'] button",
    ]

    date_field = None
    for selector in date_selectors:
        try:
            date_field = WebDriverWait(driver, timeout // 2).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            break
        except TimeoutException:
            continue

    if date_field is None:
        xpath_candidates = [
            "//button[contains(translate(., 'GİDİŞDEPARTURE', 'gidişdeparture'), 'gidiş')]",
            "//button[contains(translate(., 'GIDIS', 'gidis'), 'gidis')]",
        ]
        for xpath in xpath_candidates:
            try:
                date_field = WebDriverWait(driver, timeout // 2).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                break
            except TimeoutException:
                continue

    if date_field is not None:
        try:
            date_field.click()
        except Exception:
            pass
    else:
        departure_input = find_date_input(driver, role="departure")
        if departure_input is not None:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", departure_input)
                driver.execute_script("arguments[0].focus();", departure_input)
            except Exception:
                pass
        else:
            raise RuntimeError("Unable to locate the departure date selector.")

    apply_date_selection(driver, departure_date, timeout)

    if return_date:
        apply_date_selection(driver, return_date, timeout, is_return=True)

    # Close the calendar if it's still open (press escape)
    try:
        driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    except Exception:
        pass


def apply_date_selection(driver: webdriver.Chrome, date_str: str, timeout: int, is_return: bool = False) -> None:
    """
    Attempt to choose the provided date in the calendar. If specific day buttons cannot be located
    the function falls back to populating the underlying input via JavaScript.
    """
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date format ({date_str}). Expected YYYY-MM-DD.") from exc

    day_button_selector = f"button[data-day='{target_date.day}'][data-month='{target_date.month}']"
    alternative_selector = f"button[aria-label*='{target_date.strftime('%d')}'][aria-label*='{target_date.strftime('%B')}']"

    for selector in (day_button_selector, alternative_selector):
        try:
            day_button = WebDriverWait(driver, timeout // 2).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            day_button.click()
            return
        except TimeoutException:
            continue

    # Fallback: populate the underlying input field directly
    target_input = find_date_input(driver, role="return" if is_return else "departure")
    if not target_input:
        raise RuntimeError(
            f"Unable to locate {'return' if is_return else 'departure'} date input for fallback assignment."
        )

    formatted = target_date.strftime("%d.%m.%Y")
    driver.execute_script("arguments[0].focus();", target_input)
    target_input.click()
    target_input.send_keys(Keys.CONTROL, "a")
    target_input.send_keys(Keys.DELETE)
    target_input.send_keys(formatted)
    target_input.send_keys(Keys.ENTER)


def trigger_search(driver: webdriver.Chrome, timeout: int) -> None:
    search_selectors = [
        "button[data-testid*='search-button']",
        "button[type='submit']",
        "button[class*='search']",
        "form button",
    ]
    for selector in search_selectors:
        try:
            button = WebDriverWait(driver, timeout // 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
            button.click()
            return
        except TimeoutException:
            continue
        except Exception:
            continue

    # Final fallback: submit the form with Enter
    try:
        driver.switch_to.active_element.send_keys(Keys.RETURN)
    except Exception as exc:
        raise RuntimeError("Unable to trigger flight search action.") from exc


def wait_for_results(driver: webdriver.Chrome, timeout: int) -> None:
    card_selectors = [
        "[data-testid*='flight-card']",
        "[data-testid*='result-card']",
        "article[data-testid*='result']",
        "article[data-testid*='flight']",
        "div[data-testid*='flight-card']",
    ]
    for selector in card_selectors:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return
        except TimeoutException:
            continue
    # As a last resort wait for any article element to appear in the results grid
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "main article, div[data-component*='flight-card'], li[data-testid*='flight']")
        )
    )


def extract_flight_cards(driver: webdriver.Chrome) -> List[WebElement]:
    selectors = [
        "[data-testid='flight-card']",
        "[data-testid^='flight-card-']",
        "[data-testid*='result-card']",
        "article[data-testid*='flight']",
        "article[data-testid*='result']",
        "div[data-testid*='flight-card']",
    ]
    seen = set()
    cards: List[WebElement] = []
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            ref_id = element.get_attribute("data-testid") or element.id
            if ref_id in seen:
                continue
            seen.add(ref_id)
            cards.append(element)

    if not cards:
        cards = driver.find_elements(By.CSS_SELECTOR, "article")

    return cards


LocatorList = Sequence[Tuple[str, str]]


def get_first_match_text(element: WebElement, locators: LocatorList) -> str:
    for by_, locator in locators:
        try:
            target = element.find_element(by_, locator)
            text = target.text.strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def parse_price(text: str) -> Optional[float]:
    normalized = text.replace(".", "").replace("TL", "").replace("₺", "").replace(",", ".")
    digits = "".join(ch for ch in normalized if ch.isdigit() or ch == ".")
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def parse_duration(duration_text: str) -> Optional[int]:
    if not duration_text:
        return None
    duration_text = duration_text.lower().replace(",", ".")
    hours = 0
    minutes = 0

    # Turkish formats e.g., "1s 20d", "2sa 30dk"
    hour_tokens = ["saat", "sa", "h"]
    minute_tokens = ["dakika", "dk", "d"]

    tokens = duration_text.split()
    for idx, token in enumerate(tokens):
        cleaned = token.replace(",", ".")
        next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if any(next_token.startswith(ht) for ht in hour_tokens):
            try:
                hours = int(float(cleaned))
            except ValueError:
                continue
        elif any(next_token.startswith(mt) for mt in minute_tokens):
            try:
                minutes = int(float(cleaned))
            except ValueError:
                continue

    if hours == 0 and minutes == 0:
        # Attempt to extract digits arbitrarily
        digits = "".join(ch if ch.isdigit() else " " for ch in duration_text).split()
        if digits:
            if len(digits) >= 2:
                hours = int(digits[0])
                minutes = int(digits[1])
            elif len(digits) == 1:
                hours = int(digits[0])

    total_minutes = hours * 60 + minutes
    return total_minutes or None


def simplify_connection_text(text: str) -> str:
    if not text:
        return ""
    lowercase = text.lower()
    if "aktarmasız" in lowercase or "direct" in lowercase:
        return "Non-stop"
    if "1" in lowercase and "aktar" in lowercase:
        return "1 Stop"
    if "2" in lowercase and "aktar" in lowercase:
        return "2 Stops"
    if "3" in lowercase and "aktar" in lowercase:
        return "3 Stops"
    if "aktar" in lowercase:
        return "Connecting"
    return text.strip()


def extract_flight_record(card: WebElement) -> Optional[FlightRecord]:
    departure_time = get_first_match_text(
        card,
        [
            (By.CSS_SELECTOR, "[data-testid*='departure-time']"),
            (By.CSS_SELECTOR, "[class*='departure'] [class*='time']"),
            (By.CSS_SELECTOR, "[data-testid*='takeoff']"),
            (By.CSS_SELECTOR, "time[data-testid*='departure']"),
        ],
    )
    arrival_time = get_first_match_text(
        card,
        [
            (By.CSS_SELECTOR, "[data-testid*='arrival-time']"),
            (By.CSS_SELECTOR, "[class*='arrival'] [class*='time']"),
            (By.CSS_SELECTOR, "time[data-testid*='arrival']"),
        ],
    )
    airline = get_first_match_text(
        card,
        [
            (By.CSS_SELECTOR, "[data-testid*='airline-name']"),
            (By.CSS_SELECTOR, "[class*='airline'] span"),
            (By.CSS_SELECTOR, "[class*='carrier'] span"),
        ],
    )
    price_text = get_first_match_text(
        card,
        [
            (By.CSS_SELECTOR, "[data-testid*='price']"),
            (By.CSS_SELECTOR, "[class*='price'] span"),
            (By.CSS_SELECTOR, "[class*='price'] strong"),
        ],
    )
    if not price_text:
        price_text = card.text

    price_value = parse_price(price_text)
    if price_value is None:
        return None

    connection_info = get_first_match_text(
        card,
        [
            (By.CSS_SELECTOR, "[data-testid*='connection-info']"),
            (By.CSS_SELECTOR, "[data-testid*='leg-info']"),
            (By.CSS_SELECTOR, "[class*='connection']"),
            (
                By.XPATH,
                ".//*[contains(translate(normalize-space(.),"
                " 'ABCDEFGHIJKLMNOPQRSTUVWXYZÇĞİÖŞÜ',"
                " 'abcdefghijklmnopqrstuvwxyzçğıöşü'), 'aktar')]",
            ),
        ],
    )
    duration_text = get_first_match_text(
        card,
        [
            (By.CSS_SELECTOR, "[data-testid*='duration']"),
            (By.CSS_SELECTOR, "[class*='duration']"),
            (
                By.XPATH,
                ".//*[contains(translate(normalize-space(.),"
                " 'ABCDEFGHIJKLMNOPQRSTUVWXYZÇĞİÖŞÜ',"
                " 'abcdefghijklmnopqrstuvwxyzçğıöşü'), 'saat')]",
            ),
        ],
    )

    # Fallback extractions
    if not airline:
        airline = extract_value_from_text(card, ["Pegasus", "Turkish", "AnadoluJet", "SunExpress"])
    if not connection_info:
        connection_info = extract_value_from_text(card, ["Aktarmasız", "1 aktarma", "2 aktarma"])
    if not duration_text:
        duration_text = extract_value_from_text(card, ["saat", "dk"])

    return FlightRecord(
        departure_time=departure_time,
        arrival_time=arrival_time,
        airline=airline or "Unknown",
        price=price_value,
        price_display=price_text.strip(),
        connection_info=simplify_connection_text(connection_info),
        duration=duration_text.strip(),
        duration_minutes=parse_duration(duration_text),
    )


def extract_value_from_text(card: WebElement, keywords: Iterable[str]) -> str:
    full_text = card.text or ""
    for keyword in keywords:
        if keyword.lower() in full_text.lower():
            return keyword
    return ""


def find_date_input(driver: webdriver.Chrome, role: str) -> Optional[WebElement]:
    role_key = role.lower()
    selector_map: Dict[str, List[Tuple[str, str]]] = {
        "departure": [
            (By.CSS_SELECTOR, "input[data-testid*='departure-date']"),
            (By.CSS_SELECTOR, "[data-testid*='departure-date'] input"),
            (By.CSS_SELECTOR, "input[name*='departure']"),
            (By.CSS_SELECTOR, "input[name*='start']"),
            (By.CSS_SELECTOR, "input[id*='departure']"),
            (By.CSS_SELECTOR, "input[placeholder*='Gidiş']"),
            (By.CSS_SELECTOR, "input[aria-label*='Gidiş']"),
            (By.CSS_SELECTOR, "input[placeholder*='gidis']"),
            (By.CSS_SELECTOR, "input[aria-label*='gidis']"),
            (By.CSS_SELECTOR, "input[data-name*='departure']"),
        ],
        "return": [
            (By.CSS_SELECTOR, "input[data-testid*='return-date']"),
            (By.CSS_SELECTOR, "[data-testid*='return-date'] input"),
            (By.CSS_SELECTOR, "input[name*='return']"),
            (By.CSS_SELECTOR, "input[name*='end']"),
            (By.CSS_SELECTOR, "input[id*='return']"),
            (By.CSS_SELECTOR, "input[placeholder*='Dönüş']"),
            (By.CSS_SELECTOR, "input[aria-label*='Dönüş']"),
            (By.CSS_SELECTOR, "input[placeholder*='donus']"),
            (By.CSS_SELECTOR, "input[aria-label*='donus']"),
            (By.CSS_SELECTOR, "input[data-name*='return']"),
        ],
    }
    candidates = selector_map.get(role_key, [])
    for by_, locator in candidates:
        try:
            elements = driver.find_elements(by_, locator)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                continue
    return None


def slugify_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_value = ascii_value.lower()
    ascii_value = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return ascii_value or "unknown"


def build_search_url(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
    origin_slug: Optional[str],
    destination_slug: Optional[str],
) -> str:
    origin_part = origin_slug or slugify_name(origin)
    destination_part = destination_slug or slugify_name(destination)

    # Known overrides for frequently requested cities/airports
    overrides = {
        "istanbul": "istanbul",
        "istanbul (avrupa)": "istanbul",
        "istanbul (anadolu)": "istanbul-saw",
        "İstanbul": "istanbul",
        "istanbul-saw": "istanbul-saw",
        "lefkosa": "lefkosa",
        "lefkoşa": "lefkosa",
        "nicosia": "lefkosa",
        "ercan": "ercan",
    }

    origin_part = overrides.get(origin_part.lower(), origin_part)
    destination_part = overrides.get(destination_part.lower(), destination_part)

    path = f"{BASE_URL}{origin_part}-{destination_part}/"
    query_params = [f"gidis={departure_date}"]
    if return_date:
        query_params.append(f"donus={return_date}")
    full_url = path + ("?" + "&".join(query_params))
    print(f"[INFO] Navigating to direct search URL: {full_url}")
    return full_url


def scrape_flights(
    origin: str,
    destination: str,
    origin_slug: Optional[str],
    destination_slug: Optional[str],
    departure_date: str,
    return_date: Optional[str],
    output_path: Path,
    headless: bool,
    max_wait: int,
) -> None:
    driver = configure_driver(headless=headless)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        used_direct_url = False
        primary_exc: Optional[Exception] = None

        driver.get(BASE_URL)
        accept_cookies(driver, timeout=max_wait // 2)

        try:
            fill_route_inputs(driver, origin, destination, timeout=max_wait)
            set_dates(driver, departure_date, return_date, timeout=max_wait)
            trigger_search(driver, timeout=max_wait)
        except Exception as exc:
            primary_exc = exc
            print(f"[WARN] Form submission failed ({exc}). Attempting direct URL navigation.")
            search_url = build_search_url(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                origin_slug=origin_slug,
                destination_slug=destination_slug,
            )
            driver.get(search_url)
            accept_cookies(driver, timeout=max_wait // 2)
            used_direct_url = True

        wait_for_results(driver, timeout=max_wait)

        cards = extract_flight_cards(driver)
        records: List[FlightRecord] = []
        for card in cards:
            try:
                record = extract_flight_record(card)
                if record:
                    records.append(record)
            except Exception:
                continue

        if not records:
            if primary_exc is not None and not used_direct_url:
                raise RuntimeError(f"No flight records were extracted; initial error: {primary_exc}") from primary_exc
            raise RuntimeError("No flight records were extracted. Selectors may need updating.")

        df = pd.DataFrame(record.as_dict() for record in records)
        df.insert(0, "origin", origin)
        df.insert(1, "destination", destination)
        df.insert(2, "departure_date", departure_date)
        if return_date:
            df.insert(3, "return_date", return_date)

        df.to_csv(output_path, index=False)
        print(f"Saved {len(df)} flights to {output_path}")
    finally:
        driver.quit()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    try:
        scrape_flights(
            origin=args.origin,
            destination=args.destination,
            origin_slug=args.origin_slug,
            destination_slug=args.destination_slug,
            departure_date=args.departure_date,
            return_date=args.return_date,
            output_path=output_path,
            headless=args.headless,
            max_wait=args.max_wait,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
