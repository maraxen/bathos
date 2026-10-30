+++
id = "HYG-007"
title = "A step-back review record must be valid JSON and cite its evidence"
severity = "warning"
source_check = "check_stepback_reviews"
tags = ["hygiene", "review", "stepback"]
see_also = ["HYG-005"]
+++

A step-back review under `.bth/stepback/reviews/` is read by the advisor brief and by the
campaign hold logic. A record that is not valid JSON, does not match the review schema, or
cites fewer evidence items than the schema requires is either skipped or misread, so a hold
can rest on a review that was never checked.

**What to do.** Re-run the review, or repair the record so it parses and carries the required
fields and evidence citations. `bth lint` reports the file and the reason under
`stepback_invalid_json` or the schema issue it found. Do not hand-edit a review to clear a hold;
record a new review instead.
