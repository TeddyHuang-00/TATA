from __future__ import annotations

from provider import get_providers


def main():
    providers = get_providers()
    print(providers)
    print(providers["deepseek_reasoner"])


if __name__ == "__main__":
    main()
