"""NACHA return reason codes (subset) and what AP should do about each."""

RETURN_CODES: dict[str, tuple[str, str]] = {
    "R01": ("Insufficient funds", "Retry is not applicable to credits; contact the vendor's bank."),
    "R02": ("Account closed", "Get new bank details from the vendor through a verified channel."),
    "R03": ("No account / unable to locate account", "Bank details on file are wrong; re-verify with the vendor."),
    "R04": ("Invalid account number structure", "Account number is malformed; re-verify with the vendor."),
    "R16": ("Account frozen", "Do not resend; escalate to treasury."),
    "R20": ("Non-transaction account", "Vendor must supply a checking or savings account."),
    "R29": ("Corporate customer advises not authorized", "Confirm the payment with the vendor before resending."),
}


def describe(code: str) -> tuple[str, str]:
    return RETURN_CODES.get(code, ("Unknown return reason", "Contact the bank."))
