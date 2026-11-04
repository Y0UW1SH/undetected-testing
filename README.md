# undetected-testing

Testing [SeleniumBase](https://github.com/seleniumbase/SeleniumBase) stealth for bypassing bot-detection.

E.g. Scraping Walmart from GitHub Actions:

https://github.com/mdmintz/undetected-testing/actions/runs/37512407751/job/112436648975#step:12:13

<img width="650" alt="Image" src="https://github.com/user-attachments/assets/86545cd2-a00e-42bb-800a-53da59ca1bd0" />

--------

E.g. Scraping Etsy from GitHub Actions:

https://github.com/mdmintz/undetected-testing/actions/runs/40770290055/job/123105334967#step:13:15

<img width="660" alt="Image" src="https://github.com/user-attachments/assets/1b3c46c9-d05b-4c13-a901-532910ff6d99" />

--------

SeleniumBase CDP Mode bypasses bot-detection on multiple sites:

<img width="568" alt="Undetected SeleniumBase" src="https://user-images.githubusercontent.com/6788579/213031519-0220d19f-e210-4858-b6b5-9253b112efe4.png">

--------

## How the CI works

All checks live in one workflow, `.github/workflows/ci.yml`:

* A `lint` job runs flake8.
* A `stealth` matrix job runs groups of `raw_*.py` scripts on Ubuntu x86,
  Ubuntu ARM64, and macOS. Each group is one matrix entry with a list of
  scripts; add a script to CI by appending a line to the right group.
* Every script ends with an assertion (`sb.assert_true(...)`,
  `sb.assert_element(...)`, or a plain `assert`) that fails if the site
  blocked the browser or returned no data, so a green run means stealth
  actually worked, not just that the script exited.
* All scripts in a group run even if one fails; the failures are listed at
  the end of the step. Screenshots and logs are uploaded as artifacts.

Run a script locally the same way CI does:

```bash
pip install -r requirements.txt
seleniumbase install chromedriver
python raw_walmart.py --debug
```
