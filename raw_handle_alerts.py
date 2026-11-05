"""An example of handling alerts in CDP Mode."""
from seleniumbase import SB

with SB(uc=True, test=True, rtf=True) as sb:
    sb.activate_cdp_mode()
    sb.goto("https://the-internet.herokuapp.com/javascript_alerts")
    sb.click('button[onclick="jsAlert()"]')
    sb.sleep(1)
    sb.uc_gui_press_key("\n")  # Accept Alert
    sb.sleep(1)
    sb.assert_text("You successfully clicked an alert", "#result")
    sb.click('button[onclick="jsConfirm()"]')
    sb.sleep(1)
    sb.uc_gui_press_key("ESC")  # Dismiss Alert
    sb.sleep(1)
    sb.assert_text("You clicked: Cancel", "#result")
    sb.click('button[onclick="jsPrompt()"]')
    sb.sleep(1)
    sb.uc_gui_write("Here is my prompt answer\n")
    sb.sleep(1)
    sb.assert_text("You entered: Here is my prompt answer", "#result")
