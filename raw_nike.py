from seleniumbase import SB

with SB(uc=True, test=True, rtf=True, locale="en", pls="none") as sb:
    sb.activate_cdp_mode()
    sb.goto("https://www.nike.com/")
    sb.sleep(2.5)
    sb.click('[data-testid="user-tools-container"] search')
    sb.sleep(1.5)
    search = "Nike Air Force 1"
    sb.press_keys('input[type="search"]', search)
    sb.sleep(4)
    details = 'ul[data-testid*="products"] figure .details'
    elements = sb.select_all(details)
    sb.assert_true(len(elements) > 0, 'No results for "%s"!' % search)
    print('**** Found results for "%s": ****' % search)
    for element in elements:
        print("* " + element.text)
    sb.sleep(2)
    sb.save_screenshot_to_logs()
