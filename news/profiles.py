"""Closed industry profiles; never accept paths or secret names from HTTP input."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Profile:
    industry: str
    label: str
    route: str
    type_field: str
    channel: str

    @property
    def prefix(self):
        return f"data/{self.industry}-news/"

    @property
    def env_prefix(self):
        return self.industry.upper() + "_NEWS_"

    def env(self, suffix, default=""):
        return os.environ.get(self.env_prefix + suffix, default)

    def issue_id(self, day):
        return self.industry + "-" + day


PROFILES = {
    "wind": Profile("wind", "풍력", "daily", "wind_type", "wind-news"),
    "solar": Profile("solar", "태양광", "solar", "solar_type", "solar-daily"),
}


def profile_for(industry="wind"):
    if not isinstance(industry, str) or industry not in PROFILES:
        raise ValueError("unsupported_news_industry")
    return PROFILES[industry]


def bind_config(config, industry):
    """Reject mixed profiles before any collection, storage or external action."""
    profile = profile_for(industry)
    config["industry"] = industry
    sections = [config["collection"], config["policy"],
                config["policy"].setdefault("publisher", {}),
                config["policy"].setdefault("summarizer", {})]
    for section in sections:
        if section.get("industry", industry) != industry:
            raise ValueError("news_profile_mismatch")
        section["industry"] = industry
    return profile
