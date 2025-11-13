import pandas as pd
import yfinance as yf
import streamlit as st
from datetime import datetime
import base64

# --- User Inputs ---
stock_symbol = st.text_input("Enter Stock Ticker", "OKE")
shares = st.number_input("Number of Shares", value=100)

# Selectable date purchased, default to today
purchase_date = st.date_input("Date Purchased", datetime.today())

# --- Fetch stock data ---
stock = yf.Ticker(stock_symbol)

# --- Get last available close on or before purchase date ---
hist = stock.history(end=pd.Timestamp(purchase_date) + pd.Timedelta(days=1))
if hist.empty:
    st.error("No stock price data available for the selected purchase date.")
    st.stop()
stock_price = hist['Close'][-1]

# --- Dividend series ---
div_series = stock.dividends
if not div_series.empty:
    if div_series.index.tz is None:
        div_series = div_series.tz_localize("America/New_York")

    # --- Calculate dividend frequency from last 12 months ---
    one_year_ago = pd.Timestamp(purchase_date).tz_localize("America/New_York") - pd.DateOffset(years=1)
    recent_divs = div_series[div_series.index >= one_year_ago]
    div_freq = len(recent_divs) if len(recent_divs) > 0 else 1

    # --- Determine yearly dividend ---
    yearly_dividend = recent_divs.sum() if not recent_divs.empty else 0

    # --- Determine next dividend date ---
    next_div_date = div_series[div_series.index > pd.Timestamp(purchase_date).tz_localize("America/New_York")].index.min()
    if pd.isna(next_div_date):
        last_div = div_series.index.max()
        if div_freq >= 12:  # monthly
            next_div_date = last_div + pd.DateOffset(days=30)
        elif div_freq == 6:  # semiannual
            next_div_date = last_div + pd.DateOffset(days=180)
        else:  # quarterly by default
            next_div_date = last_div + pd.DateOffset(days=90)
else:
    div_freq = 1
    yearly_dividend = 0
    next_div_date = None

# --- Timestamp for calculations ---
today = pd.Timestamp(purchase_date).tz_localize("America/New_York")

all_options = []

# --- Identify expirations 10 to 18 months out ---
available_exps = stock.options
filtered_exps = []
for exp_str in available_exps:
    exp_date = pd.Timestamp(exp_str).tz_localize("America/New_York")
    if 10*30 <= (exp_date - today).days <= 18*30:  # approx 10–18 months
        filtered_exps.append(exp_date)

if not filtered_exps:
    st.warning("No expirations available between 10 and 18 months from purchase date.")
    st.stop()

# --- Loop through filtered expirations ---
for option_exp in filtered_exps:
    try:
        opt_chain = stock.option_chain(option_exp.strftime('%Y-%m-%d')).calls
    except Exception as e:
        st.warning(f"Error fetching option chain for {option_exp.date()}: {e}")
        continue

    if opt_chain.empty:
        st.warning(f"No call options for expiration {option_exp.date()}")
        continue

    # --- Filter ITM strikes: 10%–40% below stock price ---
    lower_bound = stock_price * 0.6
    upper_bound = stock_price * 0.9
    opt_chain = opt_chain[(opt_chain['strike'] >= lower_bound) & (opt_chain['strike'] <= upper_bound)]
    if opt_chain.empty:
        st.warning(f"No ITM options 10–40% below stock price for expiration {option_exp.date()}")
        continue

    # --- Common calculations ---
    opt_chain['Option Price'] = (opt_chain['bid'] + opt_chain['ask']) / 2
    opt_chain['Net Debit'] = stock_price - opt_chain['Option Price']
    opt_chain['Option Premium'] = opt_chain['strike'] + opt_chain['Option Price'] - stock_price
    opt_chain['Open Interest'] = opt_chain['openInterest']

    # --- Days Held ---
    days_held = max((option_exp - today).days, 1)
    opt_chain['Days Held'] = days_held
    opt_chain['Option Expiration'] = option_exp.date()

    # --- Dividend at strike ---
    opt_chain['Dividend at Strike Price'] = (yearly_dividend / opt_chain['strike']) * 100

    # --- Calculate actual dividends during full holding period ---
    # Project future dividend dates based on historical payment pattern
    
    # Get the most recent historical dividend dates to establish the pattern
    recent_div_dates = div_series[div_series.index >= one_year_ago].index
    
    if len(recent_div_dates) >= 2:
        # Calculate average days between dividend payments
        date_diffs = [(recent_div_dates[i] - recent_div_dates[i-1]).days for i in range(1, len(recent_div_dates))]
        avg_days_between = sum(date_diffs) / len(date_diffs)
    else:
        # Fallback to frequency-based estimate
        avg_days_between = 365.25 / div_freq if div_freq > 0 else 365.25
    
    # Project dividend dates forward from the last known dividend
    last_known_div = div_series.index.max()
    projected_div_dates = []
    next_div = last_known_div + pd.Timedelta(days=avg_days_between)
    
    # Project until we're past the option expiration
    while next_div <= option_exp:
        projected_div_dates.append(next_div)
        next_div = next_div + pd.Timedelta(days=avg_days_between)
    
    # Count dividends that fall after purchase date and on or before expiration
    divs_in_period = [d for d in projected_div_dates if d > today and d <= option_exp]
    expected_div_payments = len(divs_in_period)
    
    # Calculate total dividends
    single_dividend = yearly_dividend / div_freq if div_freq > 0 else 0
    divs_during_period = single_dividend * expected_div_payments
    
    # DEBUG
    st.write(f"=== Expiration {option_exp.date()} ===")
    st.write(f"Last known dividend: {last_known_div.date()}")
    st.write(f"Avg days between dividends: {avg_days_between:.1f}")
    st.write(f"Projected dividend dates in holding period:")
    for d in divs_in_period:
        st.write(f"  - {d.date()}")
    st.write(f"Number of dividend payments: {expected_div_payments}")
    st.write(f"Single dividend: ${single_dividend:.4f}")
    st.write(f"Total dividends: ${divs_during_period:.4f}")
    st.write(f"===")

    # --- Scenario: Hold Dividend (hold to expiration, receive all dividends) ---
    hold = pd.DataFrame(index=opt_chain.index)
    hold['Dividend + Premium'] = (opt_chain['Option Premium'].values * shares) + (divs_during_period * shares)
    hold['Total %'] = hold['Dividend + Premium'] / (shares * opt_chain['Net Debit'].values) * 100
    hold['Annualized %'] = hold['Total %'] * (365 / days_held)

    # --- Scenario: Called Early (called one week before last dividend) ---
    # For early call, assume called one week before the last dividend payment
    if len(divs_in_period) > 0:
        # Get the last dividend date
        last_div_date = divs_in_period[-1]
        
        # Early call happens one week (7 days) before the last dividend
        early_call_date = last_div_date - pd.Timedelta(days=7)
        
        # Count how many dividends occur before the early call date
        early_divs = [d for d in divs_in_period if d < early_call_date]
        expected_payments_early = len(early_divs)
        divs_received_early = single_dividend * expected_payments_early
        
        days_held_early = max((early_call_date - today).days, 1)
    else:
        # No dividends - early call is same as hold scenario
        expected_payments_early = 0
        divs_received_early = 0
        early_call_date = option_exp
        days_held_early = days_held

    # DEBUG
    st.write(f"Early call scenario:")
    st.write(f"  Last dividend date: {divs_in_period[-1].date() if len(divs_in_period) > 0 else 'N/A'}")
    st.write(f"  Early call date (1 week before last div): {early_call_date.date()}")
    st.write(f"  Dividends received if called early: {expected_payments_early}")
    st.write(f"  Total dividends: ${divs_received_early:.4f}")
    st.write(f"  Days held: {days_held_early}")

    early = pd.DataFrame(index=opt_chain.index)
    early['Dividend + Premium'] = (opt_chain['Option Premium'].values * shares) + (divs_received_early * shares)
    early['Total %'] = early['Dividend + Premium'] / (shares * opt_chain['Net Debit'].values) * 100
    early['Annualized %'] = early['Total %'] * (365 / days_held_early)

    # --- Premium after one dividend payment (for reference) ---
    opt_chain['Premium - Single Dividend'] = opt_chain['Option Premium'] - single_dividend

    # --- Combine scenarios into final DataFrame ---
    combined = pd.DataFrame({
        'Date Purchased': today.date(),
        'Stock': stock_symbol,
        'Stock Price': stock_price,
        'Forward Dividend $': yearly_dividend,
        'Forward Dividend %': (yearly_dividend / stock_price) * 100,
        'Dividend Frequency': div_freq,
        'Next Dividend Date': next_div_date.date() if next_div_date is not None else None,
        'Option Expiration': option_exp.date(),
        'Strike': opt_chain['strike'],
        'Option Price': opt_chain['Option Price'],
        'Net Debit': opt_chain['Net Debit'],
        'Option Premium': opt_chain['Option Premium'],
        'Open Interest': opt_chain['openInterest'],
        'Premium - Single Dividend': opt_chain['Premium - Single Dividend'],
        'Dividend at Strike Price': opt_chain['Dividend at Strike Price'],
        'Hold Dividend: Dividend + Premium': hold['Dividend + Premium'],
        'Hold Dividend: Total %': hold['Total %'],
        'Hold Dividend: Annualized %': hold['Annualized %'],
        'Called Early: Dividend + Premium': early['Dividend + Premium'],
        'Called Early: Total %': early['Total %'],
        'Called Early: Annualized %': early['Annualized %']
    })

    all_options.append(combined)

# --- Combine all expirations ---
if all_options:
    final_df = pd.concat(all_options, ignore_index=True)
else:
    st.warning("No ITM options 10–40% below stock price found in the 10–18 month window.")
    st.stop()

# --- Format % columns ---
pct_cols = [
    'Forward Dividend %',
    'Dividend at Strike Price',
    'Hold Dividend: Total %','Hold Dividend: Annualized %',
    'Called Early: Total %','Called Early: Annualized %'
]
for col in pct_cols:
    final_df[col] = final_df[col].map(lambda x: f"{x:.2f}%")

# --- Display columns ---
display_cols = [
    'Date Purchased','Stock','Stock Price','Forward Dividend $','Forward Dividend %',
    'Dividend Frequency','Next Dividend Date',
    'Option Expiration','Strike','Option Price','Net Debit','Option Premium','Premium - Single Dividend',
    'Dividend at Strike Price','Open Interest',
    'Hold Dividend: Dividend + Premium','Hold Dividend: Total %','Hold Dividend: Annualized %',
    'Called Early: Dividend + Premium','Called Early: Total %','Called Early: Annualized %'
]

# --- Highlight best ROI row ---
best_idx = final_df['Hold Dividend: Total %'].str.rstrip('%').astype(float).idxmax()
def highlight_best_row(x):
    return ['background-color: lightgreen' if i == best_idx else '' for i in x.index]

st.subheader(f"{stock_symbol} Buy-Write Dashboard (10–18 Months, ITM 10–40% below stock)")
st.dataframe(final_df[display_cols].style.apply(highlight_best_row, axis=1))

# --- Best Overall Option ---
best_option_df = pd.DataFrame([final_df.loc[best_idx]])
st.subheader("Best Overall Option (Hold Dividend scenario)")
st.dataframe(best_option_df[display_cols])

# --- Download CSV ---
def get_table_download_link(df, filename="options_data.csv"):
    csv = df.to_csv(index=False)
    b64 = base64.b64encode(csv.encode()).decode()
    href = f'<a href="data:file/csv;base64,{b64}" download="{filename}">Download CSV</a>'
    return href

st.markdown(get_table_download_link(final_df), unsafe_allow_html=True)
st.markdown(get_table_download_link(best_option_df, filename="best_option.csv"), unsafe_allow_html=True)