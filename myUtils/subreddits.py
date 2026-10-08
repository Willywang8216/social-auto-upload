"""What each subreddit will actually accept, and whether our content fits.

Why this exists
---------------
Reddit punished us for guessing. Between 2026-10-07 and 10-08 the publisher
collected, against nine configured subreddits:

  * ``SUBREDDIT_NOTALLOWED_BANNED`` on r/GayBros (3x) and r/GayBody - the account
    is banned there, so every submit was a wasted attempt and a further strike.
  * ``SUBMIT_VALIDATION_LINK_WHITELIST`` on r/NudistMen - a link post to a
    non-whitelisted domain.
  * ``NO_SELFS`` on r/gaybrosgonemild - it does not allow text posts.
  * ``RATELIMIT`` on r/gaybrosgonemild (2x) - repeated submits to a sub we are
    not welcome in.

None of that is random. Each subreddit publishes rules and each has a submission
mode and an audience that either match our content or do not:

  * our content is **nude or suggestive male-body video, plus sex-positive talk**
    for Sexualwill (SW) and naturist body content for Nakedwill (NW),
  * six of the nine configured SW subreddits are **meme or discussion** subs
    (r/gay_irl, r/gaymemes, r/gaymers, r/GayMen, r/lgbt, r/gaybrosgonemild) that
    ban NSFW imagery and ban self-promotion. Posting there cannot succeed.

What this module adds
---------------------
``SubredditProfile`` records, per subreddit, the facts that decide the outcome:
submission mode, NSFW stance, whether self-promotion is allowed, flair and title
requirements, karma/age gates, and whether a **monetised** account is refused -
which matters because both accounts here promote an adult brand.

``SubredditRegistry`` answers the three questions the publisher asks:

  * ``fits(platform_content)`` - should we attempt this at all,
  * ``denial_reason(...)``  - why we must not, in the words of the rule,
  * ``recommendations(...)`` - which subreddits would accept this content.

Nothing here mutates state; it is a knowledge table the publisher consults so a
submit that cannot work is refused locally instead of burning a retry budget and
risking the account.

Sources: the public ``/r/<sub>/about`` and ``/about/rules`` endpoints, read live
(see ``scripts/subreddit_audit.py``), so the table can be re-verified rather than
trusted forever.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


# What a piece of media actually contains. The publisher knows which of these
# applies; the registry decides whether a subreddit tolerates it.
NUDITY = "nudity"                 # bare body, naturist
EXPLICIT = "explicit"             # genital-focused or sexual act
SEX_POSITIVE = "sex_positive"     # talk/news about sexuality, no explicit image
SUGGESTIVE = "suggestive"         # shirtless, mild
SFW = "sfw"                       # non-sexual


@dataclass(frozen=True)
class SubredditProfile:
    """The facts about one subreddit that decide whether our content fits."""

    name: str
    subscribers: int = 0
    over18: bool = False
    submission_type: str = "any"      # "any" | "link" | "self" | "media"
    accepts_nudity: bool = False
    accepts_explicit: bool = False
    accepts_suggestive: bool = False
    accepts_sex_positive: bool = False
    self_promotion: str = "restricted"  # "allowed" | "restricted" | "banned"
    monetised_accounts: str = "restricted"  # "allowed" | "restricted" | "banned"
    requires_flair: bool = False
    title_pattern: str | None = None  # regex a title must match, when enforced
    min_karma: int = 0
    notes: str = ""
    # Set once the platform itself has banned this account here. A soft ban is
    # per-account and only observable by trying, so it is recorded rather than
    # inferred.
    banned: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# The table. Only subreddits we have either configured or verified live belong
# here - an unknown subreddit is treated as "unknown", never as "fine".
# ---------------------------------------------------------------------------
_PROFILES: tuple[SubredditProfile, ...] = (
    # --- accepts our NW content -------------------------------------------------
    SubredditProfile(
        name="NudistMen",
        subscribers=67065,
        over18=True,
        submission_type="any",
        accepts_nudity=True,
        self_promotion="restricted",
        monetised_accounts="restricted",
        requires_flair=True,
        notes="Flaccid naturist content only; no erection, no genital focus. "
              "Link posts must use a whitelisted domain - publish as a self post.",
    ),
    SubredditProfile(
        name="nudists",
        subscribers=294447,
        over18=True,
        submission_type="any",
        accepts_nudity=True,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        notes="Largest naturist sub and the best reach for NW content, but both "
              "monetised accounts and any promotion are refused outright.",
    ),
    SubredditProfile(
        name="GayBody",
        subscribers=55384,
        over18=True,
        submission_type="any",
        accepts_nudity=True,
        accepts_explicit=False,
        self_promotion="restricted",
        monetised_accounts="restricted",
        requires_flair=True,
        banned=True,
        notes="Body-appreciation sub. Account ryanbossom is banned here.",
    ),
    SubredditProfile(
        name="BareMenPositivity",
        subscribers=3561,
        over18=True,
        submission_type="any",
        accepts_nudity=True,
        self_promotion="restricted",
        monetised_accounts="restricted",
        requires_flair=True,
        notes="Full-body only, no sexual framing, must have substance.",
    ),
    # --- accepts adult-male nudity, but not promotion ---------------------------
    SubredditProfile(
        name="softies",
        subscribers=375990,
        over18=True,
        submission_type="link",
        accepts_nudity=True,
        self_promotion="banned",
        monetised_accounts="banned",
        notes="Flaccid only. Explicitly refuses adult-creator accounts; good "
              "reach for the content, unusable for a brand account.",
    ),
    SubredditProfile(
        name="bigonewild",
        subscribers=289651,
        over18=True,
        submission_type="any",
        accepts_nudity=True,
        accepts_explicit=True,
        self_promotion="banned",
        monetised_accounts="banned",
        notes="Nude media required; CIS men only; no paid content; removes posts "
              "under 10 upvotes in 10h.",
    ),
    SubredditProfile(
        name="gaybears",
        subscribers=242452,
        over18=True,
        submission_type="link",
        accepts_nudity=True,
        self_promotion="restricted",
        monetised_accounts="restricted",
        requires_flair=True,
    ),
    SubredditProfile(
        name="GayChubs",
        subscribers=197551,
        over18=True,
        submission_type="link",
        accepts_nudity=True,
        self_promotion="restricted",
        monetised_accounts="restricted",
    ),
    SubredditProfile(
        name="gaysiansgonewild",
        subscribers=98217,
        over18=True,
        submission_type="any",
        accepts_nudity=True,
        accepts_explicit=True,
        self_promotion="restricted",
        monetised_accounts="restricted",
    ),
    SubredditProfile(
        name="gaybrosgonemild",
        subscribers=322875,
        over18=False,
        submission_type="link",
        accepts_nudity=False,
        accepts_suggestive=True,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        notes="Requires your face in frame and no nudity; bans adult-subscription "
              "promotion; TEXT posts are refused (NO_SELFS). Wrong sub for us.",
    ),
    SubredditProfile(
        name="gaymersgonemild",
        subscribers=66118,
        over18=False,
        submission_type="link",
        accepts_nudity=False,
        accepts_suggestive=True,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
    ),
    # --- discussion / meme subs that cannot carry our content -------------------
    SubredditProfile(
        name="lgbt",
        subscribers=1302466,
        submission_type="any",
        accepts_nudity=False,
        accepts_sex_positive=True,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        notes="Huge reach but no advertisements or self-promotion, NSFW limited, "
              "no generative-AI content.",
    ),
    SubredditProfile(
        name="gaybros",
        subscribers=543248,
        submission_type="any",
        accepts_nudity=False,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        banned=True,
        notes="No porn, no selfies, no self-promotion. Account is banned here.",
    ),
    SubredditProfile(
        name="AskGaybros",
        subscribers=777508,
        submission_type="self",
        accepts_nudity=False,
        accepts_sex_positive=True,
        self_promotion="banned",
        monetised_accounts="banned",
        notes="Text questions only - a video cannot be submitted.",
    ),
    SubredditProfile(
        name="gay_irl",
        subscribers=226503,
        submission_type="link",
        accepts_nudity=False,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        title_pattern=r"^(gay|trans|bi|ace|les).+irl$",
        notes="Meme sub. Title must match gay/trans/bi/ace/les...irl. No self-promo.",
    ),
    SubredditProfile(
        name="gaymemes",
        subscribers=65396,
        submission_type="any",
        accepts_nudity=False,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        notes="Meme sub; no AI-generated content.",
    ),
    SubredditProfile(
        name="GayMen",
        subscribers=79062,
        submission_type="any",
        accepts_nudity=False,
        accepts_sex_positive=True,
        self_promotion="banned",
        monetised_accounts="banned",
        requires_flair=True,
        notes="No NSFW images or videos, no selfies, no eye candy, no self-promo.",
    ),
    SubredditProfile(
        name="gaymers",
        subscribers=313711,
        submission_type="any",
        accepts_nudity=False,
        self_promotion="restricted",
        monetised_accounts="banned",
        notes="Gaming sub. No selfies, no porn; self-promotion limited.",
    ),
    # --- promotion-friendly, useful for building an audience --------------------
    SubredditProfile(
        name="OnlyFansAdvice",
        subscribers=587327,
        submission_type="any",
        accepts_nudity=False,
        accepts_sex_positive=True,
        # The rules say "NSFW creators only" AND "No advertising or networking".
        # So the audience is exactly right, but a brand publish is not what this
        # sub is for: it is for asking and giving advice.
        self_promotion="banned",
        monetised_accounts="allowed",
        requires_flair=True,
        notes="NSFW creators only, and advertising is banned. Participate with real "
              "advice questions rather than publishing media.",
    ),
    SubredditProfile(
        name="NSFW411",
        subscribers=1464775,
        submission_type="self",
        accepts_nudity=False,
        accepts_sex_positive=True,
        self_promotion="banned",
        monetised_accounts="restricted",
        requires_flair=True,
        notes="Directory for finding NSFW subs. Rule: 'No Advertising'. Text "
              "requests only - not a publishing target.",
    ),
)


class SubredditRegistry:
    """Look up subreddit requirements and decide whether content fits."""

    def __init__(self, profiles: tuple[SubredditProfile, ...] = _PROFILES) -> None:
        self._by_name: dict[str, SubredditProfile] = {}
        for profile in profiles:
            self._by_name[profile.name.lower().lstrip("r/")] = profile

    # -- lookup ---------------------------------------------------------------
    def get(self, name: str) -> SubredditProfile | None:
        return self._by_name.get(str(name).strip().lower().lstrip("r/"))

    def known(self, name: str) -> bool:
        return self.get(name) is not None

    # -- fit ------------------------------------------------------------------
    def denial_reason(
        self,
        name: str,
        *,
        content_kind: str = SFW,
        monetised: bool = True,
        title: str | None = None,
        karma: int | None = None,
        submission_kind: str | None = None,
        require_known: bool = True,
    ) -> str | None:
        """Why this subreddit must not be attempted, or ``None`` if it may be.

        Returning a reason rather than a bare False keeps the operator informed:
        the message names the subreddit and the rule, so a configuration mistake
        is fixed instead of retried.

        ``require_known`` defaults to True - publishing to a subreddit nobody has
        verified is what produced the r/GayBros and r/gaybrosgonemild failures.
        Callers that only want the rules applied to a name they already
        understand (tests, and the title/flair helpers) may pass False.
        """
        profile = self.get(name)
        if profile is None:
            if not require_known:
                return None
            # An unverified subreddit is a risk, not a pass.
            return (
                f"r/{name} has no verified requirement profile. Add it to "
                "myUtils/subreddits.py from a live audit (scripts/subreddit_audit.py) "
                "before publishing to it."
            )
        if profile.banned:
            return (
                f"r/{name} has banned this account (SUBREDDIT_NOTALLOWED_BANNED). "
                "Remove it from the account's subreddits; every submit here is a "
                "wasted attempt and another strike."
            )
        # Submission mode next: it is a hard, mechanical constraint, and reporting
        # "this sub refuses a monetised brand" when the real problem is that it
        # takes links only would send the operator to fix the wrong thing.
        if submission_kind == "self" and profile.submission_type == "link":
            return (
                f"r/{name} accepts link posts only, so a text/self post is refused "
                "(NO_SELFS)."
            )
        if submission_kind == "link" and profile.submission_type == "self":
            return f"r/{name} accepts text posts only, so a link/media post is refused."
        if title is not None and profile.title_pattern:
            import re

            if not re.match(profile.title_pattern, title.strip(), re.IGNORECASE):
                return (
                    f"r/{name} enforces a title format ({profile.title_pattern}); "
                    f"{title!r} does not match."
                )
        if monetised and profile.monetised_accounts == "banned":
            return (
                f"r/{name} refuses accounts that promote a monetised brand. "
                "Post from a non-brand account or remove this subreddit."
            )
        if profile.self_promotion == "banned":
            return (
                f"r/{name} bans self-promotion, which is what a brand publish is. "
                "Use it for participation only, not for publishing."
            )
        if content_kind == EXPLICIT and not profile.accepts_explicit:
            return f"r/{name} does not accept explicit content."
        if content_kind == NUDITY and not profile.accepts_nudity:
            return f"r/{name} does not accept nudity."
        if content_kind == SEX_POSITIVE and not (
            profile.accepts_sex_positive or profile.accepts_nudity
        ):
            return f"r/{name} is not a sex-positive community."
        if (
            karma is not None
            and profile.min_karma
            and karma < profile.min_karma
        ):
            return (
                f"r/{name} requires at least {profile.min_karma} karma; this "
                f"account has {karma}."
            )
        return None

    def fits(self, name: str, **kwargs) -> bool:
        return self.denial_reason(name, **kwargs) is None

    # -- guidance -------------------------------------------------------------
    def recommendations(
        self, *, content_kind: str = SFW, monetised: bool = True
    ) -> list[SubredditProfile]:
        """Subreddits that would accept this content, largest audience first."""
        out = [
            profile
            for profile in self._by_name.values()
            if self.denial_reason(
                profile.name, content_kind=content_kind, monetised=monetised
            )
            is None
        ]
        return sorted(out, key=lambda p: p.subscribers, reverse=True)

    def audit(self, names: list[str], **kwargs) -> list[tuple[str, str | None]]:
        """Pair every configured subreddit with its denial reason (or None)."""
        return [(name, self.denial_reason(name, **kwargs)) for name in names]


_DEFAULT_REGISTRY = SubredditRegistry()


def get_registry() -> SubredditRegistry:
    return _DEFAULT_REGISTRY


def audit_subreddits(names: list[str], **kwargs) -> list[tuple[str, str | None]]:
    return _DEFAULT_REGISTRY.audit(names, **kwargs)


def recommendations(*, content_kind: str = SFW, monetised: bool = True):
    return _DEFAULT_REGISTRY.recommendations(
        content_kind=content_kind, monetised=monetised
    )
