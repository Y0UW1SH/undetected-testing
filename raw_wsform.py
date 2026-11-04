from seleniumbase import sb_cdp

sb = sb_cdp.Chrome(locale="en", incognito=True)
sb.goto("https://wsform.com/demo/")
sb.sleep(3)
sb.solve_captcha()
sb.sleep(3)
sb.assert_true(
    "Just a moment" not in sb.get_title(), "Still on the challenge page!"
)
sb.assert_element("form")
sb.save_screenshot_to_logs()
