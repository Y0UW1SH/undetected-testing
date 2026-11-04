from seleniumbase import SB

with SB(test=True) as sb:
    url = "https://my-user-agent.com/"
    sb.goto(url)
    sb.highlight("#ua_string")
    user_agent_detected = sb.get_text("#ua_string")
    print("\n\nUser-Agent: %s\n\n" % user_agent_detected)
    sb.assert_true("Mozilla" in user_agent_detected, "Bad User-Agent!")
    sb.assert_false(
        "HeadlessChrome" in user_agent_detected, "Headless UA leaked!"
    )
