from seleniumbase import Driver

driver = Driver()
driver.get("https://api.ipify.org")
ip_address = driver.find_element("css selector", "body").text.strip()
print(ip_address)
assert ip_address.count(".") == 3, "Expected an IPv4 address: %r" % ip_address
driver.quit()
