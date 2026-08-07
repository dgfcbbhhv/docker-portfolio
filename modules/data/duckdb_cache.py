"""DuckDB本地数据缓存模块
参考 EasyXT 的 DuckDB 数据存储方案，大幅提升全市场扫描速度（约10倍）
"""
import os, logging
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DUCKDB_AVAILABLE = False
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "stock_data.ddb")

def _ensure_db():
    if not DUCKDB_AVAILABLE:
        return None
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_quotes (
            code VARCHAR,
            date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            amount DOUBLE,
            PRIMARY KEY (code, date)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS spot_data (
            code VARCHAR PRIMARY KEY,
            name VARCHAR,
            price DOUBLE,
            chg DOUBLE,
            pe DOUBLE,
            pb DOUBLE,
            mcap DOUBLE,
            amount DOUBLE,
            update_time TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS financial_data (
            code VARCHAR,
            report_date DATE,
            eps DOUBLE,
            bps DOUBLE,
            roe DOUBLE,
            debt_ratio DOUBLE,
            revenue DOUBLE,
            net_profit DOUBLE,
            PRIMARY KEY (code, report_date)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS dividends (
            code VARCHAR,
            date DATE,
            dividend DOUBLE,
            dividend_type VARCHAR,
            PRIMARY KEY (code, date)
        )
    """)
    return con

def save_daily_quotes(code, df):
    if not DUCKDB_AVAILABLE:
        return False
    try:
        con = _ensure_db()
        if con is None:
            return False
        
        df = df.copy()
        df["code"] = code
        df["date"] = pd.to_datetime(df["date"])
        
        con.execute("""
            INSERT OR REPLACE INTO daily_quotes 
            SELECT code, date, open, high, low, close, volume, amount 
            FROM df
        """)
        con.commit()
        logger.info(f"Saved {len(df)} daily quotes for {code}")
        return True
    except Exception as e:
        logger.error(f"Failed to save daily quotes for {code}: {e}")
        return False

def load_daily_quotes(code, start_date=None, end_date=None):
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = _ensure_db()
        if con is None:
            return None
        
        query = "SELECT * FROM daily_quotes WHERE code = ?"
        params = [code]
        
        if start_date:
            query += " AND date >= ?"
            params.append(pd.to_datetime(start_date))
        if end_date:
            query += " AND date <= ?"
            params.append(pd.to_datetime(end_date))
        
        query += " ORDER BY date"
        
        df = con.execute(query, params).fetchdf()
        if not df.empty:
            logger.info(f"Loaded {len(df)} daily quotes for {code}")
            return df
        return None
    except Exception as e:
        logger.error(f"Failed to load daily quotes for {code}: {e}")
        return None

def save_spot_data(df):
    if not DUCKDB_AVAILABLE:
        return False
    try:
        con = _ensure_db()
        if con is None:
            return False
        
        df = df.copy()
        df["update_time"] = pd.Timestamp.now()
        
        con.execute("DELETE FROM spot_data WHERE code IN (SELECT code FROM df)")
        con.execute("""
            INSERT INTO spot_data (code, name, price, chg, pe, pb, mcap, amount, update_time)
            SELECT c, n, price, chg, pe, pb, mcap, amount, update_time FROM df
        """)
        con.commit()
        logger.info(f"Saved {len(df)} spot records")
        return True
    except Exception as e:
        logger.error(f"Failed to save spot data: {e}")
        return False

def load_spot_data(codes=None, max_age_hours=24):
    """
    加载实时行情缓存，默认有效期24小时。
    过期数据返回 None 表示需要重新获取。
    """
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = _ensure_db()
        if con is None:
            return None
        
        if codes:
            query = "SELECT * FROM spot_data WHERE code IN (" + ",".join(["?"] * len(codes)) + ")"
            df = con.execute(query, codes).fetchdf()
        else:
            df = con.execute("SELECT * FROM spot_data").fetchdf()
        
        if not df.empty:
            # 检查数据新鲜度
            age_hours = 0
            if "update_time" in df.columns:
                now = pd.Timestamp.now()
                oldest = df["update_time"].min()
                age_hours = (now - oldest).total_seconds() / 3600
                if age_hours > max_age_hours:
                    logger.info(f"Spot data expired (age={age_hours:.1f}h > {max_age_hours}h), refreshing...")
                    return None
            logger.info(f"Loaded {len(df)} spot records (age={age_hours:.1f}h)")
            return df
        return None
    except Exception as e:
        logger.error(f"Failed to load spot data: {e}")
        return None

def save_financial_data(code, df):
    if not DUCKDB_AVAILABLE:
        return False
    try:
        con = _ensure_db()
        if con is None:
            return False
        
        df = df.copy()
        df["code"] = code
        
        con.execute("DELETE FROM financial_data WHERE code = ?", [code])
        con.execute("""
            INSERT INTO financial_data (code, report_date, eps, bps, roe, debt_ratio, revenue, net_profit)
            SELECT code, report_date, eps, bps, roe, debt_ratio, revenue, net_profit FROM df
        """)
        con.commit()
        logger.info(f"Saved financial data for {code}")
        return True
    except Exception as e:
        logger.error(f"Failed to save financial data for {code}: {e}")
        return False

def load_financial_data(code):
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = _ensure_db()
        if con is None:
            return None
        
        df = con.execute("SELECT * FROM financial_data WHERE code = ? ORDER BY report_date DESC", [code]).fetchdf()
        if not df.empty:
            return df
        return None
    except Exception as e:
        logger.error(f"Failed to load financial data for {code}: {e}")
        return None

def get_stock_list():
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = _ensure_db()
        if con is None:
            return None
        
        df = con.execute("SELECT DISTINCT code FROM spot_data").fetchdf()
        if not df.empty:
            return df["code"].tolist()
        return []
    except Exception as e:
        logger.error(f"Failed to get stock list: {e}")
        return []

def get_last_update_time():
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = _ensure_db()
        if con is None:
            return None
        
        result = con.execute("SELECT MAX(update_time) as last_time FROM spot_data").fetchdf()
        if not result.empty and pd.notna(result["last_time"].iloc[0]):
            return result["last_time"].iloc[0]
        return None
    except Exception as e:
        logger.error(f"Failed to get last update time: {e}")
        return None

def delete_old_data(days=365):
    if not DUCKDB_AVAILABLE:
        return
    try:
        con = _ensure_db()
        if con is None:
            return
        
        cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=days)
        con.execute("DELETE FROM daily_quotes WHERE date < ?", [cutoff_date])
        con.commit()
        logger.info(f"Deleted data older than {days} days")
    except Exception as e:
        logger.error(f"Failed to delete old data: {e}")

def optimize_db():
    if not DUCKDB_AVAILABLE:
        return
    try:
        con = _ensure_db()
        if con is None:
            return
        
        con.execute("PRAGMA optimize")
        con.commit()
        logger.info("Database optimized")
    except Exception as e:
        logger.error(f"Failed to optimize database: {e}")

def is_available():
    return DUCKDB_AVAILABLE

def get_db_stats():
    if not DUCKDB_AVAILABLE:
        return None
    try:
        con = _ensure_db()
        if con is None:
            return None
        
        stats = {}
        stats["quotes_count"] = con.execute("SELECT COUNT(*) FROM daily_quotes").fetchdf().iloc[0, 0]
        stats["spot_count"] = con.execute("SELECT COUNT(*) FROM spot_data").fetchdf().iloc[0, 0]
        stats["financial_count"] = con.execute("SELECT COUNT(*) FROM financial_data").fetchdf().iloc[0, 0]
        stats["dividend_count"] = con.execute("SELECT COUNT(*) FROM dividends").fetchdf().iloc[0, 0]
        
        return stats
    except Exception as e:
        logger.error(f"Failed to get database stats: {e}")
        return None
