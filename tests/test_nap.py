from seo import models
from seo.local import nap


def test_normalize_phone():
    assert nap.normalize_phone("(512) 555-0100") == "5125550100"
    assert nap.normalize_phone("+1 512.555.0100") == "5125550100"
    assert nap.normalize_phone("555-0100") is None


def test_extract_phones_dedupes():
    text = "Call (512) 555-0100 or 512-555-0100, fax 512 555 0199."
    assert nap.extract_phones(text) == ["5125550100", "5125550199"]


def test_address_matches_with_abbreviations():
    ref = "123 Main Street, Suite 4, Austin, TX 78701"
    assert nap.address_matches(ref, "Located at 123 Main St Austin TX 78701")
    assert nap.address_matches(ref, "123 Main St, Austin 78702") is False
    assert nap.address_matches(ref, "456 Main St 78701") is False
    assert nap.address_matches("", "anything") is None
    # "st" must match the word, not the substring in "Austin"
    assert nap.address_matches("123 Main St", "123 Austin Rd") is False


def test_directory_detection_and_filtering():
    assert nap.directory_for("https://www.yelp.com/biz/joes") == "Yelp"
    assert nap.directory_for("https://m.yelp.com/biz/joes") == "Yelp"
    assert nap.directory_for("https://joesplumbing.example/") is None
    results = [
        models.SerpResult("q", "", "desktop", i + 1, "organic", url,
                          None, None)
        for i, url in enumerate([
            "https://joesplumbing.example/",
            "https://www.yelp.com/biz/joes",
            "https://www.yelp.com/biz/joes-2",
            "https://www.bbb.org/us/tx/austin/profile/joes",
        ])
    ]
    picked = nap.filter_directory_results(results)
    assert [label for label, _ in picked] == ["Yelp", "BBB"]
    assert picked[0][1].url == "https://www.yelp.com/biz/joes"


def test_audit_page(fixture):
    rec = nap.audit_page(fixture("yelp_listing.html"), "Joe's Plumbing LLC",
                         "Yelp", "https://www.yelp.com/biz/joes",
                         phone="512-555-0100",
                         address="123 Main St, Austin, TX 78701")
    assert rec.phone_match is True
    assert rec.address_match is True
    assert rec.name_found.startswith("Joe's Plumbing LLC")
    # script contents are stripped before phone extraction
    assert "5120000000" not in rec.phone_found
    assert rec.phone_found.startswith("5125550100")
    wrong = nap.audit_page(fixture("yelp_listing.html"), "Joe's", "Yelp",
                           "u", phone="512-555-0199",
                           address="999 Other Rd 11111")
    assert wrong.phone_match is False and wrong.address_match is False
