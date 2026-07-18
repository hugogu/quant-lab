-- 002_seed_symbols.sql — optional starter symbol list (10 A股)
INSERT INTO symbol(symbol, name, market, exchange, list_date) VALUES
    ('000001', '平安银行',     'astock', 'SZ', '1991-04-03'),
    ('600519', '贵州茅台',     'astock', 'SH', '2001-08-27'),
    ('000858', '五粮液',       'astock', 'SZ', '1998-04-16'),
    ('002594', '比亚迪',       'astock', 'SZ', '2011-06-30'),
    ('300750', '宁德时代',     'astock', 'SZ', '2018-06-11'),
    ('601318', '中国平安',     'astock', 'SH', '2007-03-01'),
    ('600036', '招商银行',     'astock', 'SH', '2002-04-09'),
    ('601398', '工商银行',     'astock', 'SH', '2006-10-27'),
    ('601288', '农业银行',     'astock', 'SH', '2010-07-15'),
    ('000333', '美的集团',     'astock', 'SZ', '2013-09-18')
ON CONFLICT (symbol) DO NOTHING;
