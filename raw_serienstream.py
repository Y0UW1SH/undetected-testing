from seleniumbase import sb_cdp

sb = sb_cdp.Chrome(guest=True, ad_block=True)
url = "https://serienstream.to/serie/24/staffel-1/episode-1"
sb.goto(url)
sb.sleep(4)
sb.solve_captcha()
sb.sleep(3)
sb.assert_true(
    "Just a moment" not in sb.get_title(), "Still on the challenge page!"
)
sb.assert_url_contains("serienstream.to/serie/")
sb.save_screenshot_to_logs()
