# Kraken read-only API key

This guide sets up a Kraken API key that can only **look at** your Kraken spot account:
balances, fee tier and orders. It cannot trade, cancel, deposit, withdraw or move money.
Crypto Radar uses it only in the account check command below. The
radar loop does not use it.

## 1. Create the key on Kraken

1. Sign in at <https://www.kraken.com>.
2. Click your profile icon (top right), then **Settings**, then the **API** tab.
3. Click **Create API key**.
4. In **Key name**, type `crypto-radar read only`.
5. Tick **only** these permissions:
   - Funds: **Query Funds**
   - Orders and trades: **Query Open Orders & Trades**
   - Orders and trades: **Query Closed Orders & Trades**
6. Leave **every other box unticked**. In particular, these must stay off:
   - Deposit Funds
   - Withdraw Funds
   - Create & Modify Orders
   - Cancel & Close Orders
   - any transfer, staking or Earn permission
   - Access WebSockets API
7. Leave the other settings as they are.
8. Click **Generate key**.
9. Kraken shows two values: the **API Key** and the **Private Key**. Keep this page open
   for step 2. Kraken shows the Private Key only once.

Check the permission list on the key once more before you continue. If you see
Withdraw, Create & Modify Orders or Cancel & Close Orders ticked, delete the key and
start again.

## 2. Save the key in the project folder

The key goes into a file inside the project folder (the folder that holds `radar.py`).
That folder is ignored by Git, so the file is never committed.

Do **not** put the key in Windows environment variables: the radar refuses to start
when a Kraken key is in its environment.

1. Open PowerShell.
2. Go to the project folder (replace the path with where you cloned the repository):

   ```powershell
   cd path\to\crypto-radar
   ```

3. Create the `.kraken` folder and open a new file in Notepad:

   ```powershell
   New-Item -ItemType Directory -Force .kraken
   notepad .kraken\credentials.json
   ```

4. Notepad asks whether to create the file. Click **Yes**.
5. Paste this text, then replace the two placeholders with the values from Kraken
   (keep the quotes):

   ```json
   {
     "api_key": "PASTE THE API KEY HERE",
     "api_secret": "PASTE THE PRIVATE KEY HERE"
   }
   ```

6. Save (Ctrl+S) and close Notepad.
7. Back on the Kraken page, close the key dialog.

## 3. Run the account check

In the same PowerShell window, in the project folder:

```powershell
python scripts\kraken_account_check.py --mode SHADOW_LIVE
```

It shows your balances, your trade balance in EUR, your 30-day volume with the taker
and maker fee for BTC/EUR, and your open orders (order ids are shortened). It only
reads. For another pair's fee, add `--pair ETHEUR`, for example.

What the messages mean:

| Message | What to do |
| --- | --- |
| `UNAVAILABLE: credentials_missing` | The file is not at `.kraken\credentials.json` in the project folder. Repeat step 2. |
| `UNAVAILABLE: credentials_malformed` or `api_secret_not_base64` | The file text is wrong. Open it again with `notepad .kraken\credentials.json` and compare it with step 2.5. |
| `FAILED at ...: INVALID_KEY` or `INVALID_SIGNATURE` | Kraken did not accept the key. Copy both values again, or create a new key. |
| `FAILED at ...: PERMISSION_DENIED` | A query permission is missing. Edit the key on Kraken and tick the three permissions of step 1.5. |
| `FAILED at ...: RATE_LIMITED` | Wait a few minutes, then run the check again. |
| `FAILED at ...: INVALID_NONCE` | Use this key only for this command, and run one check at a time. |

## 4. Revoke the key

When you no longer want the radar to read your account:

1. On Kraken: profile icon, **Settings**, **API** tab. Delete the `crypto-radar read only` key.
2. In PowerShell, in the project folder:

   ```powershell
   Remove-Item -Recurse .kraken
   ```

Deleting the key on Kraken is what stops access. Deleting the folder removes the local copy.
