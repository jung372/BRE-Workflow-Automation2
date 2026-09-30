"""Retired onboarding entry point; replaces old server copies safely."""


def main():
    raise SystemExit(
        "Naver onboarding is disabled: current Search API terms prohibit AI input "
        "and permanent storage. No credentials requested, read or saved. "
        "See https://developers.naver.com/products/terms/ (sections 2.3-2.4)."
    )


if __name__ == "__main__":
    main()
