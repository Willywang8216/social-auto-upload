# YouTube external links

YouTube metadata may link only to the creator's direct Patreon, Instagram,
Facebook and Threads profiles. Linktree/link hubs, OnlyFans, JustForFans,
Fansly, other websites, email addresses, redirect parameters and individual
nude/NSFW/content-post links are excluded. An approved brand handle such as
`NakedWill` is not itself a nude-content route; this is a URL policy, not an
assessment of all destination content or a guarantee of YouTube approval.

## Creator URLs

Set `youtubeAllowedLinks` to a list of verified HTTPS profile URLs in the
profile's `settings` JSON. An account's `config.youtubeAllowedLinks` overrides
the profile; an explicit empty list means no links. Without an explicit list,
only eligible direct URLs already in the profile's public contact/default-link
fields are used. No handles or URLs are invented.

Generation filters both inputs and outputs and adds one complete canonical
footer. Its byte budget is reserved within the 5,000-byte description limit.
Native and Sociamonials guards check the final mapped text, including tags,
and reject unsafe or partially truncated links before provider writes.
Strict recovery validates approved text without rewriting it.

## Watermarks and existing content

New media preparation uses the selected pre-overlay content for YouTube when
an automatically applied text watermark contains a disallowed URL. The same
selected intro/outro assembly is preserved. X and other main media routes keep
their branded variant. YouTube covers/screenshots avoid that generated overlay.
This does not inspect original footage, logos or destination content and does
not remove already-burned-in text from historical media. Frozen media and
receipts must not be swapped or resubmitted without guarded, authorized recovery.

Unstarted scheduled captions can be corrected with an atomic, backed-up,
compare-and-set operation on their exact job and UI rows. Keep their media,
schedule, attempts and receipts unchanged. Delivered/ambiguous jobs are not
caption-reset or repost candidates. A removed YouTube video still requires
YouTube's review/appeal process; a source fix cannot reinstate it.
