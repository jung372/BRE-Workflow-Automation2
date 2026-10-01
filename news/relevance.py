"""Shared industry scope for discovery, ingestion and editorial priority."""
import re


def industry_relevant(title, text=""):
    value = title + " " + text
    if re.search(r"풍력|풍황|모노파일|해상\s*변전소|wind\s*(power|farm|turbine)|offshore wind", value, re.I):
        return True
    # Supply-chain news need not literally contain the word 'wind'. A company
    # name alone is insufficient (for example, its unrelated stock-price news).
    return bool(re.search(r"해저케이블|하부구조물|설치선|블레이드|터빈|해상송전", value)
                and re.search(r"재생에너지|해상발전|해상\s*에너지|신재생|해상단지", value))


def editorial_order(groups):
    """Prioritize direct industry subjects and spread a finite LLM budget.

    Similar titles are deferred, never automatically merged: separate contracts
    and project stages must keep their own evidence and database records.
    """
    remaining = list(groups.items())
    selected, used, titles = [], {}, []
    def grams(title):
        value = re.sub(r"[^가-힣a-z0-9]", "", title.lower())
        return {value[i:i+3] for i in range(max(0, len(value)-2))}
    while remaining:
        def score(pair):
            article = pair[1][0]
            title = grams(article["title"])
            similarity = max((len(title & other) / max(1, min(len(title), len(other))) for other in titles), default=0)
            priority = 4 * industry_relevant(article["title"]) - 5 * similarity
            priority -= min(3, used.get(article["source_name"], 0)) * .6
            return priority, article["source_published_at"], article["url"]
        pair = max(remaining, key=score)
        remaining.remove(pair); selected.append(pair)
        article = pair[1][0]
        titles.append(grams(article["title"]))
        used[article["source_name"]] = used.get(article["source_name"], 0) + 1
    return selected
