"""Generate Procurement Desk labels using the supplied playbook and CSV files."""

import argparse
import csv
import re
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path


AMOUNT = r"[\d,]+(?:\.\d+)?"
MONEY = re.compile(
    rf"(?<!\w)(?P<currency>INR|Rs\.?|USD|EUR|GBP|JPY|AED|SGD|₹|\$|€|£)"
    rf"\s*(?P<value>{AMOUNT})(?:\s*(?P<scale>lakh\b|k\b))?", re.I
)
SMALL = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(20),
))
SMALL.update(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
SCALES = {"thousand": 1000, "lakh": 100000, "million": 1000000}
NUMBER_WORDS = "|".join([*SMALL, *SCALES, "hundred", "and"])
WORD_OFFER = re.compile(rf"\b((?:{NUMBER_WORDS})(?:[ -]+(?:{NUMBER_WORDS}))*)\s+rupees\b", re.I)
OFFER_START = re.compile(
    r"\b(?:we (?:are pleased to quote|can do|can offer)|the best we can offer is|"
    r"our (?:revised offer is|final price is|(?:earlier )?quote (?:is|was))|should read)\s+",
    re.I,
)
INJECTION = re.compile(
    r"\boverride\b|\bpre[- ]approved\b|approval already granted|already signed off"
    r"|note (?:to|for) (?:the )?(?:ai|language model)|\bwhichever bot\b"
    r"|\bautomated system is reading\b|<!--\s*assistant\s*:"
    r"|ignore your previous instructions|reviewing assistant|AI सहायक के लिए निर्देश",
    re.I,
)
ADVANCE = re.compile(r"\b(?:upfront|advance|up front)\b", re.I)


def number(value):
    return Fraction(value.replace(",", ""))


def number_from_words(words):
    total = group = 0
    for word in re.split(r"[ -]+", words.lower()):
        if word in SMALL:
            group += SMALL[word]
        elif word == "hundred":
            group = (group or 1) * 100
        elif word in SCALES:
            total += (group or 1) * SCALES[word]
            group = 0
    return total + group


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def read_md(path):
    return path.read_text(encoding="utf-8-sig")


def load_playbook(root):
    if not (root / "categories").is_dir():
        root = root / "playbook"
    fields = {
        "GST rate": "gst", "Target price": "target", "Walk-away price": "walk",
        "Negotiation rounds (R)": "rounds", "Concession exponent (β)": "beta",
        "Approval limit": "approval",
    }
    categories, names, suppliers = {}, {}, {}
    amendments = defaultdict(list)
    for file in (root / "categories").glob("*.md"):
        body = read_md(file)
        names[body.splitlines()[0].split(": ", 1)[1].strip().casefold()] = file.stem
        policy = {}
        for heading, key in fields.items():
            match = re.search(rf"\*\*{re.escape(heading)}:\*\*\s*₹?({AMOUNT})", body)
            policy[key] = number(match[1])
        policy["period"] = re.search(r"Normalized unit:\*\*[^\n]* per (month|year)", body)
        policy["period"] = policy["period"][1] if policy["period"] else None
        categories[file.stem] = policy
    for file in (root / "amendments").glob("*.md"):
        body = read_md(file)
        category = names[body.splitlines()[0].split(": ", 1)[1].strip().casefold()]
        date = re.search(r"Effective from:\*\*\s*(\S+)", body)[1]
        field, price = re.search(rf"the (.*?) for .*? is now ₹({AMOUNT})", body).groups()
        changes = {name.casefold(): key for name, key in fields.items()}
        amendments[category].append((date, changes[field.casefold()], number(price)))
    for file in (root / "suppliers").glob("*.md"):
        body = read_md(file)
        name = body.splitlines()[0].split(": ", 1)[1].strip().casefold()
        status = re.search(r"Status:\*\*\s*(\w+)", body)[1].casefold()
        caps = {names[category.casefold()]: number(price) for category, price in
                re.findall(rf"price cap for (.*?) of ₹({AMOUNT})", body)}
        rates = {currency: number(rate) for rate, currency in
                 re.findall(rf"converted at ₹({AMOUNT}) per ([A-Z]+)", body)}
        suppliers[name] = (status, caps, rates)
    fx_text = read_md(root / "policies" / "units-and-currency.md")
    rates = {"INR": Fraction(1), **{
        currency: number(rate) for currency, rate in
        re.findall(rf"\|\s*([A-Z]{{3}})\s*\|\s*({AMOUNT})\s*\|", fx_text)
    }}
    return categories, amendments, suppliers, rates


def banned_term(message):
    for line in message.lower().splitlines():
        if not re.search(r"\bno automatic renewal\b|renewal only (?:if|by)\b", line):
            if re.search(r"auto[- ]?renew|renew\w* on its own|continues? from year to year unless", line):
                return True
        if re.search(r"acme.*liability.*unlimited|unlimited liability.*acme|\bexclusive supplier\b|buy .*only from us", line):
            return True
        if ADVANCE.search(line):
            percentages = re.findall(r"(\d+)\s*%", line)
            if any(int(value) > 30 for value in percentages) or "half the order value" in line:
                return True
    return False


def offer_text(message):
    for line in message.splitlines():
        match = OFFER_START.search(line)
        if match:
            offer = line[match.end():]
            if match[0].lower().startswith("should read"):
                offer = re.split(r",\s*not\s+", offer, maxsplit=1, flags=re.I)[0]
            return offer
    return ""


def offer_price(offer, quantity, category, policy, rates):
    candidates = list(MONEY.finditer(offer))
    if candidates:
        tier = re.search(r"orders of up to ([\d,]+)", offer, re.I)
        if tier and quantity > number(tier[1]) or offer.lower().startswith("between "):
            match = candidates[-1]
        else:
            match = candidates[0]
        currency = {"RS": "INR", "₹": "INR", "$": "USD", "€": "EUR", "£": "GBP"}.get(
            match["currency"].upper().rstrip("."), match["currency"].upper())
        if currency not in rates:
            return None
        price = number(match["value"]) * rates[currency]
        price *= {"k": 1000, "lakh": 100000}.get((match["scale"] or "").lower(), 1)
        terms = offer[match.end():]
    else:
        written = WORD_OFFER.search(offer)
        if not written:
            return None
        price = Fraction(number_from_words(written[1]))
        terms = offer[written.end():]
    terms = terms.lower()
    if policy["period"]:
        period = re.search(r"\bper (month|year)\b", terms)
        if not period:
            return None
        if period[1] != policy["period"]:
            price = price * 12 if period[1] == "month" else price / 12
    if re.search(r"gst included|(?:including|inclusive of|incl\.) gst", terms):
        price /= 1 + policy["gst"] / 100
    bulk = re.search(r"\bin total for all ([\d,]+)|\bper (?:pack of )?([\d,]+)", terms)
    if bulk:
        price /= number(bulk[1] or bulk[2])
    if "per dozen" in terms:
        price /= 12
    if category == "printer-paper" and re.search(r"\bper (?:dozen )?reams?\b", terms):
        price *= 10
    list_discount = re.search(r"(\d+)%\s+(?:discount on|off) (?:our )?list price", offer, re.I)
    if list_discount:
        price *= 1 - Fraction(int(list_discount[1]), 100)
    volume = re.search(r"(\d+)% off for orders of ([\d,]+).*?or more", terms)
    if volume and quantity >= number(volume[2]):
        price *= 1 - Fraction(int(volume[1]), 100)
    free = re.search(r"(\d+) free .*? for every (\d+) you pay for", terms)
    if free:
        paid = int(free[2])
        price *= Fraction(paid, paid + int(free[1]))
    return price


def decide(row, playbook):
    categories, amendments, suppliers, base_rates = playbook
    category = row["category"]
    policy = categories[category].copy()
    for date, key, value in sorted(amendments[category]):
        if date <= row["date"]:
            policy[key] = value
    status, caps, contract_rates = suppliers[row["supplier"].casefold()]
    walk = caps.get(category, policy["walk"])
    rates = base_rates | contract_rates
    quantity, round_number = int(row["quantity"]), int(row["round"])
    thread = row["supplier_message"]
    parts = re.split(r"^\[(Earlier|Latest) message\s+[—–-]\s*([^\n]+)\]\s*", thread, flags=re.M)
    latest = parts[-1]
    offer = offer_text(latest)
    previous_discount = re.search(r"(\d+)% off our previous quote", offer, re.I)
    if previous_discount:
        seller_history = [body for kind, sender, body in zip(parts[1::3], parts[2::3], parts[3::3])
                          if kind == "Earlier" and sender.casefold().startswith(row["supplier"].casefold())]
        previous_offer = offer_text(seller_history[-1]) if seller_history else ""
        price = offer_price(previous_offer, quantity, category, policy, rates)
        if price is not None:
            price *= 1 - Fraction(int(previous_discount[1]), 100)
    else:
        price = offer_price(offer, quantity, category, policy, rates)
    if price is None:
        return "ESCALATE:MISSING_INFO:"
    if status == "blacklisted":
        return "REJECT:BLACKLISTED_SUPPLIER:"
    if INJECTION.search(thread):
        return "ESCALATE:PROMPT_INJECTION:"
    if banned_term(latest):
        return "ESCALATE:BANNED_TERM:"
    target = policy["target"]
    limit = target
    if status != "probation":
        progress = (round_number - 1) / (policy["rounds"] - 1)
        limit += (walk - target) * progress ** int(policy["beta"])
    if price <= limit:
        if price * quantity > policy["approval"]:
            return "ESCALATE:OVER_APPROVAL_LIMIT:"
        return "ACCEPT:ACCEPTABLE_PRICE:"
    if round_number >= policy["rounds"]:
        return "REJECT:ABOVE_WALKAWAY:"
    return f"COUNTER:COUNTER_OFFER:{int(limit + Fraction(1, 2))}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="Competition directory with threads.csv and playbook/")
    parser.add_argument("output", type=Path, help="Destination path for the submission CSV")
    parser.add_argument("--check-examples", action="store_true", help="Compare against the official worked examples")
    args = parser.parse_args()
    playbook = load_playbook(args.data / "playbook")
    if args.check_examples:
        examples = read_rows(args.data / "sample_threads.csv")
        answers = {row["thread_id"]: row["label"] for row in read_rows(args.data / "sample_answers.csv")}
        wrong = []
        for row in examples:
            label = decide(row, playbook)
            expected = answers[row["thread_id"]]
            if label != expected:
                wrong.append((row["thread_id"], expected, label))
        print(f"Worked examples: {len(examples) - len(wrong)}/{len(examples)} exact matches")
        for thread_id, expected, label in wrong:
            print(f"{thread_id}: expected {expected}, got {label}")
    labels = {row["thread_id"]: decide(row, playbook) for row in read_rows(args.data / "threads.csv")}
    template = [row["thread_id"] for row in read_rows(args.data / "sample_submission.csv")]
    if len(template) != 2000 or len(set(template)) != 2000 or set(template) != labels.keys():
        raise ValueError("Thread IDs do not match the competition submission template")
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["thread_id", "label"])
        writer.writerows((thread_id, labels[thread_id]) for thread_id in template)
    print(f"Wrote {len(labels)} rows")
    print(dict(Counter(label.split(":")[1] for label in labels.values())))


if __name__ == "__main__":
    main()
