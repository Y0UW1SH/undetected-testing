import os
from seleniumbase import SB


def check_ip(sb):
    ip_address = sb.get_text("body").strip()
    print("Full IP from ipify:")
    print(ip_address)
    sb.assert_true(ip_address.count(".") == 3, "Expected an IPv4 address!")


# Regular Mode (no proxy)
with SB() as sb:
    sb.open("https://api.ipify.org")
    check_ip(sb)

try:
    # Get proxy from GitHub Secrets
    proxy = os.environ["proxy_1"]
except Exception:
    proxy = None

if proxy:
    # Confirm that proxy was obtained from GitHub Secrets
    print("First part of proxy IP:")
    print(proxy.split("@")[-1].split(":")[0].split(".")[0])

# Regular Mode with proxy from GitHub Secrets
with SB(proxy=proxy) as sb:
    sb.open("https://api.ipify.org")
    check_ip(sb)

# CDP Mode with proxy from GitHub Secrets
with SB(proxy=proxy, uc=True) as sb:
    sb.activate_cdp_mode("https://api.ipify.org")
    check_ip(sb)
