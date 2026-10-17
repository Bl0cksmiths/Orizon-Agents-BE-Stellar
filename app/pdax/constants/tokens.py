"""
PDAX supported crypto tokens → network (case-sensitive, as of Aug 2024).

Use the Token Symbol exactly for crypto-in / crypto-out. Sending an asset over
the wrong network can make recovery impossible. For Orizon the relevant symbol
is USDCXLM (USDC on Stellar) — see STELLAR_TOKENS below.
"""

from __future__ import annotations

# The network most PDAX tokens move on.
ERC20 = "Ethereum (ERC20)"

TOKEN_NETWORKS: dict[str, str] = {
    "AAVE": ERC20,
    "ADA": "Cardano",
    "ALGO": "Algorand",
    "APE": ERC20,
    "AVAX": "C-chain",
    "AXS": "Ronin",
    "BAT": ERC20,
    "BCH": "Bitcoin Cash (BCH)",
    "BNB": "BNB Smart Chain (BEP20)",
    "BTC": "BTC (SegWit)",
    "COMP": ERC20,
    "DOGE": "Dogecoin",
    "DOT": "Polkadot",
    "ENJ": ERC20,
    "ETH": ERC20,
    "GALA": ERC20,
    "GMT": "Solana",
    "GRT": ERC20,
    "HBAR": "Hedera",
    "LINK": ERC20,
    "LTC": "Litecoin",
    "MANA": ERC20,
    "POL": "Polygon",
    "PAXG": ERC20,
    "PEPE": ERC20,
    "PYUSD": ERC20,
    "RON": "Ronin",
    "SAND": ERC20,
    "SHIB": ERC20,
    "SLP": "Ronin",
    "SOL": "Solana",
    "SUSHI": ERC20,
    "TON": "TON",
    "UNI": ERC20,
    "USDCSOL": "Solana",
    "USDCXLM": "Stellar",
    "USDCPOL": "Polygon",
    "USDC": ERC20,
    "USDTPOL": "Polygon",
    "USDT": ERC20,
    "USDTTRX": "Tron (TRC20)",
    "WETHPOL": "Polygon",
    "XLM": "Stellar",
    "XRP": "Ripple",
    "XTZ": "Tezos",
    "YGGRON": "Ronin",
    "CELO": ERC20,
    "OP": ERC20,
    "NEAR": ERC20,
}

# Tokens native to Stellar — Orizon's settlement network.
STELLAR_TOKENS: frozenset[str] = frozenset({"USDCXLM", "XLM"})

SUPPORTED_TOKENS: frozenset[str] = frozenset(TOKEN_NETWORKS)


def network_for(token: str) -> str | None:
    return TOKEN_NETWORKS.get(token)


def is_supported_token(token: str) -> bool:
    return token in TOKEN_NETWORKS
