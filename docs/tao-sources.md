# What each Bittensor source can prove

Use the chain to count accounts and detect movements. Use the explorer to find
interesting holders and reconstruct price and cost history. Neither a profitable
position nor a money transfer establishes that one person controls two wallets.

| Source | Useful data | Limit |
| --- | --- | --- |
| [Taostats Accounts](https://taostats.io/accounts) | Ranked rows, coldkeys, balances and update blocks. The browser can page 100 at once. | A moving leaderboard can shift between requests. A row is discovery, not a trading history. |
| [Subnet charts and holders](https://taostats.io/subnets/11/chart) | Actual investors in subnet alpha, not just metagraph node operators; subnet prices and historical OHLC. | Holders are a current view and cannot find everyone who sold out. Hourly opens are marks, not guaranteed execution prices. |
| [Account transactions and transfers](https://taostats.io/docs/understanding-taostats/accounts/single-account-page) | Subnet, coldkey, validator, alpha quantity, TAO amount, execution price and native extrinsic ID; TAO transfers have sender and recipient. | Changes of validator and cross-subnet swaps can create multiple rows. Original cost needs corroboration on each hop; rewards are not new purchases. |
| [Network staking transactions](https://taostats.io/delegation) and [TAO transfers](https://taostats.io/transfers) | Find recent active wallets without opening every account page; look out for known exchange accounts. | A TAO payment proves a payment, not transfer of a particular subnet lot or common ownership. |
| [Native blocks](https://taostats.io/blocks), [extrinsics and events](https://taostats.io/extrinsics) | Confirm what executed; distinguish transfers, stake moves and batch calls. | Requires decoding calls; multiple legs in one call are not independent winning trades. |
| [EVM blocks](https://taostats.io/evm/blocks) | Ethereum-compatible transactions, useful for an EVM-specific question. | Does not replace the native Subtensor event and stake history. |
| [Taostats Pro portfolio](https://taostats.io/docs/understanding-taostats/taostats-pro/portfolio-data) and [official MCP](https://mcp.taostats.io/) | Portfolio gain reports, subnet flows, holders and wallet activity. | Separate product/access; the MCP requires a Taostats key. No demonstrated guarantee that these P/L numbers carry original cost between arbitrary public wallets. |
| [Local Dwellir RPC](https://www.dwellir.com/docs/bittensor) | Pin a finalized height and enumerate native `System.Account` keys; read finalized events once per block for all wallets. [Bittensor is listed as archive capable](https://www.dwellir.com/public-rpc-endpoints). | The current map omits reaped/historical-only accounts and does not itself establish who has traded. Historical replay must decode calls, prices, stake units and runtime changes. A key is local only; free responses are shared across the account. |

The web collector keeps checkpoints and explicitly identifies sampled or
unavailable pages. `python -m seo.sources.tao_census --db ... --out ...
--pages 10 --page-size 100 --sample-last --delay 2` reads consecutive Accounts
pages and optionally probes the terminal page. A terminal sample does not
count skipped pages as visited.

On your PC, `python -m seo.tao_chain_census --db PATH --out SUMMARY.json
--report DOWNLOADED_REPORT.json --prompt` resumes a fixed-block native census
and joins its addresses to observed transfers from the supplied report(s).
Each public transfer receipt retains its evidence and original cost where known.
Use a new database for a new height. This command refuses to run in CI.
