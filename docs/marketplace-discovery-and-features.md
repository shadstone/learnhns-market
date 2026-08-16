# Marketplace discovery and featured listings

## Behavior

- The website homepage defaults to active, buyable listings.
- Pending inventory remains public through `/pending`, `?status=pending`, and the pending count on the homepage.
- The Bob-compatible auction API accepts `availability=available`, `pending`, or `all`.
- Omitting `availability` preserves the combined API contract, with active inventory ordered before pending inventory.
- Curated web collections use `collection=short`, `under-500`, `emoji`, or `watched`.

## Manual web features

Migration `9d5a7c1e4b20` adds web-only feature controls to active listings. The public feature card is shown only when:

1. `featured_web` is true;
2. the optional start time has passed;
3. the optional end time has not passed; and
4. the listing is still active and unexpired.

Admins use `/admin` with `MARKET_ADMIN_TOKEN` to set the label, rank, schedule, and private note. Use `Featured` for editorial placement and `Sponsored` for paid placement. A paid placement must never be presented as organic popularity or a wallet guarantee.

This release intentionally does not add Bob-specific feature placement. Wallet promotion should receive an independent approval control before it is introduced.

## Deployment order

1. Back up the production database.
2. Deploy LearnHNS Market and run `flask db upgrade`.
3. Smoke test `/`, `/pending`, `/api/v2/auctions?availability=available`, and `/api/v2/auctions?availability=pending`.
4. Verify the admin can feature and unfeature one low-risk active listing.
5. Release Bob only after the production API accepts `availability`; an older server ignores the filter while Bob filters its returned page locally, which can produce an empty first page.

## Rollback

- Bob can temporarily request `all` to recover the prior combined view.
- Web feature cards can be removed without a deployment by setting `featuredWeb` to false.
- The homepage default can be reverted independently without dropping feature columns.
- Do not downgrade the database until all application instances using the feature columns have been rolled back.

## Seller-action limitation

Pending submissions currently contain chain/listing data but no authenticated seller account or email. The UI and API can identify and explain the next seller action, and Bob can highlight locally owned listings that need action. True seller email reminders require a future explicit, privacy-reviewed association between a pending listing and the seller's account; watcher emails must not be presented as seller notifications.
