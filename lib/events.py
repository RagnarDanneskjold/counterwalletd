import os
import re
import logging
import datetime
import time
import copy
import decimal
import json
import StringIO

import grequests
import pymongo
import gevent
from PIL import Image
import lxml.html

from lib import (config, util)

D = decimal.Decimal
COMPILE_ASSET_MARKET_INFO_PERIOD = 30 * 60 #in seconds (this is every 30 minutes currently)


def expire_stale_prefs(mongo_db):
    """
    Every day, clear out preferences objects that haven't been touched in > 30 days, in order to reduce abuse risk/space consumed
    """
    min_last_updated = time.mktime((datetime.datetime.utcnow() - datetime.timedelta(days=30)).timetuple())
    
    num_stale_records = mongo_db.preferences.find({'last_touched': {'$lt': min_last_updated}}).count()
    mongo_db.preferences.remove({'last_touched': {'$lt': min_last_updated}})
    if num_stale_records: logging.warn("REMOVED %i stale preferences objects" % num_stale_records)
    
    #call again in 1 day
    gevent.spawn_later(86400, expire_stale_prefs, mongo_db)


def expire_stale_btc_open_order_records(mongo_db):
    min_when_created = time.mktime((datetime.datetime.utcnow() - datetime.timedelta(days=15)).timetuple())
    
    num_stale_records = mongo_db.btc_open_orders.find({'when_created': {'$lt': min_when_created}}).count()
    mongo_db.btc_open_orders.remove({'when_created': {'$lt': min_when_created}})
    if num_stale_records: logging.warn("REMOVED %i stale BTC open order objects" % num_stale_records)
    
    #call again in 1 day
    gevent.spawn_later(86400, expire_stale_btc_open_order_records, mongo_db)


def compile_extended_asset_info(mongo_db):
    #create directory if it doesn't exist
    imageDir = os.path.join(config.data_dir, config.SUBDIR_ASSET_IMAGES)
    if not os.path.exists(imageDir):
        os.makedirs(imageDir)
        
    assets_info = mongo_db.asset_extended_info.find()
    for asset_info in assets_info:
        if asset_info.get('disabled', False):
            logging.info("ExtendedAssetInfo: Skipping disabled asset %s" % asset_info['asset'])
            continue
        
        #try to get the data at the specified URL
        assert 'url' in asset_info and util.is_valid_url(asset_info['url'], suffix='.json')
        data = {}
        raw_image_data = None
        try:
            #TODO: Right now this loop makes one request at a time. Fully utilize grequests to make batch requests
            # at the same time (using map() and throttling) 
            r = grequests.map((grequests.get(asset_info['url'], timeout=1, stream=True),), stream=True)[0]
            if not r: raise Exception("Invalid response")
            if r.status_code != 200: raise Exception("Got non-successful response code of: %s" % r.status_code)
            #read up to 4KB and try to convert to JSON
            raw_data = r.raw.read(4 * 1024, decode_content=True)
            r.raw.release_conn()
            data = json.loads(raw_data)
            #if here, we have valid json data
            if 'asset' not in data:
                raise Exception("Missing asset field")
            if 'description' not in data:
                data['description'] = ''
            if 'image' not in data:
                data['image'] = ''
            if 'website' not in data:
                data['website'] = ''
                
            if data['asset'] != asset_info['asset']:
                raise Exception("asset field is invalid (is: '%s', should be: '%s')" % (data['asset'], asset_info['asset']))
            if data['image'] and not util.is_valid_url(data['image']):
                raise Exception("'image' field is not valid URL")
            if data['website'] and not util.is_valid_url(data['website']):
                raise Exception("'website' field is not valid URL")
            
            if data['image']:
                #fetch the image data (must be 32x32 png, max 20KB size)
                r = grequests.map((grequests.get(data['image'], timeout=1, stream=True),), stream=True)[0]
                if not r: raise Exception("Invalid response")
                if r.status_code != 200: raise Exception("Got non-successful response code of: %s" % r.status_code)
                #read up to 20KB and try to convert to JSON
                raw_image_data = r.raw.read(20 * 1024, decode_content=True)
                r.raw.release_conn()
                try:
                    image = Image.open(StringIO.StringIO(raw_image_data))
                except:
                    raise Exception("Unable to parse image data at: %s" % data['image'])
                if image.format != 'PNG': raise Exception("Image is not a PNG: %s (got %s)" % (data['image'], image.format))
                if image.size != (48, 48): raise Exception("Image size is not 48x48: %s (got %s)" % (data['image'], image.size))
                if image.mode not in ['RGB', 'RGBA']: raise Exception("Image mode is not RGB/RGBA: %s (got %s)" % (data['image'], image.mode))
        except Exception, e:
            logging.info("ExtendedAssetInfo: Skipped asset %s: %s" % (asset_info['asset'], e))
        else:
            #sanitize any text in description to remove potential attack vector
            sanitized_description = lxml.html.document_fromstring(data['description']).text_content()
            asset_info['description'] = sanitized_description
            asset_info['website'] = data['website']
            asset_info['image'] = data['image']
            if data['image'] and raw_image_data:
                #save the image to disk
                f = open(os.path.join(imageDir, data['asset'] + '.png'), 'wb')
                f.write(raw_image_data)
                f.close()
            mongo_db.asset_extended_info.save(asset_info)
            logging.debug("ExtendedAssetInfo: Compiled data for asset %s" % asset_info['asset'])
        
    #call again in 60 minutes
    gevent.spawn_later(60 * 60, compile_extended_asset_info, mongo_db)


def compile_asset_market_info(mongo_db):
    """
    Every 10 minutes, run through all assets and compose and store market ranking information.
    This event handler is only run for the first time once we are caught up
    """
    def calc_inverse(quantity):
        return float( (D(1) / D(quantity) ).quantize(
            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))            

    def calc_price_change(open, close):
        return float((D(100) * (D(close) - D(open)) / D(open)).quantize(
                D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))            
    
    def get_price_primatives(start_dt=None, end_dt=None):
        mps_xcp_btc = util.get_market_price_summary(mongo_db, 'XCP', 'BTC', start_dt=start_dt, end_dt=end_dt)
        xcp_btc_price = mps_xcp_btc['market_price'] if mps_xcp_btc else None # == XCP/BTC
        btc_xcp_price = calc_inverse(mps_xcp_btc['market_price']) if mps_xcp_btc else None #BTC/XCP
        return mps_xcp_btc, xcp_btc_price, btc_xcp_price
    
    def get_asset_info(asset, at_dt=None):
        asset_info = mongo_db.tracked_assets.find_one({'asset': asset})
        
        if asset not in ('XCP', 'BTC') and at_dt and asset_info['_at_block_time'] > at_dt:
            #get the asset info at or before the given at_dt datetime
            for e in reversed(asset_info['_history']): #newest to oldest
                if e['_at_block_time'] <= at_dt:
                    asset_info = e
                    break
            else: #asset was created AFTER at_dt
                asset_info = None
            if asset_info is None: return None
            assert asset_info['_at_block_time'] <= at_dt
          
        #modify some of the properties of the returned asset_info for BTC and XCP
        if asset == 'BTC':
            if at_dt:
                start_block_index, end_block_index = util.get_block_indexes_for_dates(mongo_db, end_dt=at_dt)
                asset_info['total_issued'] = util.get_btc_supply(normalize=False, at_block_index=end_block_index)
                asset_info['total_issued_normalized'] = util.normalize_quantity(asset_info['total_issued'])
            else:
                asset_info['total_issued'] = util.get_btc_supply(normalize=False)
                asset_info['total_issued_normalized'] = util.normalize_quantity(asset_info['total_issued'])
        elif asset == 'XCP':
            #BUG: this does not take end_dt (if specified) into account. however, the deviation won't be too big
            # as XCP doesn't deflate quickly at all, and shouldn't matter that much since there weren't any/much trades
            # before the end of the burn period (which is what is involved with how we use at_dt with currently)
            asset_info['total_issued'] = util.call_jsonrpc_api("get_xcp_supply", [], abort_on_error=True)['result']
            asset_info['total_issued_normalized'] = util.normalize_quantity(asset_info['total_issued'])
        if not asset_info:
            raise Exception("Invalid asset: %s" % asset)
        return asset_info
    
    def get_xcp_btc_price_info(asset, mps_xcp_btc, xcp_btc_price, btc_xcp_price, with_last_trades=0, start_dt=None, end_dt=None):
        if asset not in ['BTC', 'XCP']:
            #get price data for both the asset with XCP, as well as BTC
            price_summary_in_xcp = util.get_market_price_summary(mongo_db, asset, 'XCP',
                with_last_trades=with_last_trades, start_dt=start_dt, end_dt=end_dt)
            price_summary_in_btc = util.get_market_price_summary(mongo_db, asset, 'BTC',
                with_last_trades=with_last_trades, start_dt=start_dt, end_dt=end_dt)

            #aggregated (averaged) price (expressed as XCP) for the asset on both the XCP and BTC markets
            if price_summary_in_xcp: # no trade data
                price_in_xcp = price_summary_in_xcp['market_price']
                if xcp_btc_price:
                    aggregated_price_in_xcp = float(((D(price_summary_in_xcp['market_price']) + D(xcp_btc_price)) / D(2)).quantize(
                        D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
                else: aggregated_price_in_xcp = None
            else:
                price_in_xcp = None
                aggregated_price_in_xcp = None
                
            if price_summary_in_btc: # no trade data
                price_in_btc = price_summary_in_btc['market_price']
                if btc_xcp_price:
                    aggregated_price_in_btc = float(((D(price_summary_in_btc['market_price']) + D(btc_xcp_price)) / D(2)).quantize(
                        D('.00000000'), rounding=decimal.ROUND_HALF_EVEN))
                else: aggregated_price_in_btc = None
            else:
                aggregated_price_in_btc = None
                price_in_btc = None
        else:
            #here we take the normal XCP/BTC pair, and invert it to BTC/XCP, to get XCP's data in terms of a BTC base
            # (this is the only area we do this, as BTC/XCP is NOT standard pair ordering)
            price_summary_in_xcp = mps_xcp_btc #might be None
            price_summary_in_btc = copy.deepcopy(mps_xcp_btc) if mps_xcp_btc else None #must invert this -- might be None
            if price_summary_in_btc:
                price_summary_in_btc['market_price'] = calc_inverse(price_summary_in_btc['market_price'])
                price_summary_in_btc['base_asset'] = 'BTC'
                price_summary_in_btc['quote_asset'] = 'XCP'
                for i in xrange(len(price_summary_in_btc['last_trades'])):
                    #[0]=block_time, [1]=unit_price, [2]=base_quantity_normalized, [3]=quote_quantity_normalized, [4]=block_index
                    price_summary_in_btc['last_trades'][i][1] = calc_inverse(price_summary_in_btc['last_trades'][i][1])
                    price_summary_in_btc['last_trades'][i][2], price_summary_in_btc['last_trades'][i][3] = \
                        price_summary_in_btc['last_trades'][i][3], price_summary_in_btc['last_trades'][i][2] #swap
            if asset == 'XCP':
                price_in_xcp = 1.0
                price_in_btc = price_summary_in_btc['market_price'] if price_summary_in_btc else None
                aggregated_price_in_xcp = 1.0
                aggregated_price_in_btc = btc_xcp_price #might be None
            else:
                assert asset == 'BTC'
                price_in_xcp = price_summary_in_xcp['market_price'] if price_summary_in_xcp else None
                price_in_btc = 1.0
                aggregated_price_in_xcp = xcp_btc_price #might be None
                aggregated_price_in_btc = 1.0
        return (price_summary_in_xcp, price_summary_in_btc, price_in_xcp, price_in_btc, aggregated_price_in_xcp, aggregated_price_in_btc)
        
    def calc_market_cap(asset_info, price_in_xcp, price_in_btc):
        market_cap_in_xcp = float( (D(asset_info['total_issued_normalized']) / D(price_in_xcp)).quantize(
            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN) ) if price_in_xcp else None
        market_cap_in_btc = float( (D(asset_info['total_issued_normalized']) / D(price_in_btc)).quantize(
            D('.00000000'), rounding=decimal.ROUND_HALF_EVEN) ) if price_in_btc else None
        return market_cap_in_xcp, market_cap_in_btc
    

    def compile_summary_market_info(asset, mps_xcp_btc, xcp_btc_price, btc_xcp_price):        
        """Returns information related to capitalization, volume, etc for the supplied asset(s)
        NOTE: in_btc == base asset is BTC, in_xcp == base asset is XCP
        @param assets: A list of one or more assets
        """
        asset_info = get_asset_info(asset)
        (price_summary_in_xcp, price_summary_in_btc, price_in_xcp, price_in_btc, aggregated_price_in_xcp, aggregated_price_in_btc
        ) = get_xcp_btc_price_info(asset, mps_xcp_btc, xcp_btc_price, btc_xcp_price, with_last_trades=30)
        market_cap_in_xcp, market_cap_in_btc = calc_market_cap(asset_info, price_in_xcp, price_in_btc)
        return {
            'price_in_xcp': price_in_xcp, #current price of asset vs XCP (e.g. how many units of asset for 1 unit XCP)
            'price_in_btc': price_in_btc, #current price of asset vs BTC (e.g. how many units of asset for 1 unit BTC)
            'price_as_xcp': calc_inverse(price_in_xcp) if price_in_xcp else None, #current price of asset AS XCP
            'price_as_btc': calc_inverse(price_in_btc) if price_in_btc else None, #current price of asset AS BTC
            'aggregated_price_in_xcp': aggregated_price_in_xcp, 
            'aggregated_price_in_btc': aggregated_price_in_btc,
            'aggregated_price_as_xcp': calc_inverse(aggregated_price_in_xcp) if aggregated_price_in_xcp else None, 
            'aggregated_price_as_btc': calc_inverse(aggregated_price_in_btc) if aggregated_price_in_btc else None,
            'total_supply': asset_info['total_issued_normalized'], 
            'market_cap_in_xcp': market_cap_in_xcp,
            'market_cap_in_btc': market_cap_in_btc,
        }

    def compile_24h_market_info(asset):        
        asset_data = {}
        start_dt_1d = datetime.datetime.utcnow() - datetime.timedelta(days=1)

        #perform aggregation to get 24h statistics
        #TOTAL volume and count across all trades for the asset (on ALL markets, not just XCP and BTC pairings)
        _24h_vols = {'vol': 0, 'count': 0}
        _24h_vols_as_base = mongo_db.trades.aggregate([
            {"$match": {
                "base_asset": asset,
                "block_time": {"$gte": start_dt_1d } }},
            {"$project": {
                "base_quantity_normalized": 1 #to derive volume
            }},
            {"$group": {
                "_id":   1,
                "vol":   {"$sum": "$base_quantity_normalized"},
                "count": {"$sum": 1},
            }}
        ])
        _24h_vols_as_base = {} if not _24h_vols_as_base['ok'] \
            or not len(_24h_vols_as_base['result']) else _24h_vols_as_base['result'][0]
        _24h_vols_as_quote = mongo_db.trades.aggregate([
            {"$match": {
                "quote_asset": asset,
                "block_time": {"$gte": start_dt_1d } }},
            {"$project": {
                "quote_quantity_normalized": 1 #to derive volume
            }},
            {"$group": {
                "_id":   1,
                "vol":   {"$sum": "quote_quantity_normalized"},
                "count": {"$sum": 1},
            }}
        ])
        _24h_vols_as_quote = {} if not _24h_vols_as_quote['ok'] \
            or not len(_24h_vols_as_quote['result']) else _24h_vols_as_quote['result'][0]
        _24h_vols['vol'] = _24h_vols_as_base.get('vol', 0) + _24h_vols_as_quote.get('vol', 0) 
        _24h_vols['count'] = _24h_vols_as_base.get('count', 0) + _24h_vols_as_quote.get('count', 0) 
        
        #XCP market volume with stats
        if asset != 'XCP':
            _24h_ohlc_in_xcp = mongo_db.trades.aggregate([
                {"$match": {
                    "base_asset": "XCP",
                    "quote_asset": asset,
                    "block_time": {"$gte": start_dt_1d } }},
                {"$project": {
                    "unit_price": 1,
                    "base_quantity_normalized": 1 #to derive volume
                }},
                {"$group": {
                    "_id":   1,
                    "open":  {"$first": "$unit_price"},
                    "high":  {"$max": "$unit_price"},
                    "low":   {"$min": "$unit_price"},
                    "close": {"$last": "$unit_price"},
                    "vol":   {"$sum": "$base_quantity_normalized"},
                    "count": {"$sum": 1},
                }}
            ])
            _24h_ohlc_in_xcp = {} if not _24h_ohlc_in_xcp['ok'] \
                or not len(_24h_ohlc_in_xcp['result']) else _24h_ohlc_in_xcp['result'][0]
            if _24h_ohlc_in_xcp: del _24h_ohlc_in_xcp['_id']
        else:
            _24h_ohlc_in_xcp = {}
            
        #BTC market volume with stats
        if asset != 'BTC':
            _24h_ohlc_in_btc = mongo_db.trades.aggregate([
                {"$match": {
                    "base_asset": "BTC",
                    "quote_asset": asset,
                    "block_time": {"$gte": start_dt_1d } }},
                {"$project": {
                    "unit_price": 1,
                    "base_quantity_normalized": 1 #to derive volume
                }},
                {"$group": {
                    "_id":   1,
                    "open":  {"$first": "$unit_price"},
                    "high":  {"$max": "$unit_price"},
                    "low":   {"$min": "$unit_price"},
                    "close": {"$last": "$unit_price"},
                    "vol":   {"$sum": "$base_quantity_normalized"},
                    "count": {"$sum": 1},
                }}
            ])
            _24h_ohlc_in_btc = {} if not _24h_ohlc_in_btc['ok'] \
                or not len(_24h_ohlc_in_btc['result']) else _24h_ohlc_in_btc['result'][0]
            if _24h_ohlc_in_btc: del _24h_ohlc_in_btc['_id']
        else:
            _24h_ohlc_in_btc = {}
            
        return {
            '24h_summary': _24h_vols,
            #^ total quantity traded of that asset in all markets in last 24h
            '24h_ohlc_in_xcp': _24h_ohlc_in_xcp,
            #^ quantity of asset traded with BTC in last 24h
            '24h_ohlc_in_btc': _24h_ohlc_in_btc,
            #^ quantity of asset traded with XCP in last 24h
            '24h_vol_price_change_in_xcp': calc_price_change(_24h_ohlc_in_xcp['open'], _24h_ohlc_in_xcp['close'])
                if _24h_ohlc_in_xcp else None,
            #^ aggregated price change from 24h ago to now, expressed as a signed float (e.g. .54 is +54%, -1.12 is -112%)
            '24h_vol_price_change_in_btc': calc_price_change(_24h_ohlc_in_btc['open'], _24h_ohlc_in_btc['close'])
                if _24h_ohlc_in_btc else None,
        }
    
    def compile_7d_market_info(asset):        
        start_dt_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)

        #get XCP and BTC market summarized trades over a 7d period (quantize to hour long slots)
        _7d_history_in_xcp = None # xcp/asset market (or xcp/btc for xcp or btc)
        _7d_history_in_btc = None # btc/asset market (or btc/xcp for xcp or btc)
        if asset not in ['BTC', 'XCP']:
            for a in ['XCP', 'BTC']:
                _7d_history = mongo_db.trades.aggregate([
                    {"$match": {
                        "base_asset": a,
                        "quote_asset": asset,
                        "block_time": {"$gte": start_dt_7d }
                    }},
                    {"$project": {
                        "year":  {"$year": "$block_time"},
                        "month": {"$month": "$block_time"},
                        "day":   {"$dayOfMonth": "$block_time"},
                        "hour":  {"$hour": "$block_time"},
                        "unit_price": 1,
                        "base_quantity_normalized": 1 #to derive volume
                    }},
                    {"$sort": {"block_time": pymongo.ASCENDING}},
                    {"$group": {
                        "_id":   {"year": "$year", "month": "$month", "day": "$day", "hour": "$hour"},
                        "price": {"$avg": "$unit_price"},
                        "vol":   {"$sum": "$base_quantity_normalized"},
                    }},
                ])
                _7d_history = [] if not _7d_history['ok'] else _7d_history['result']
                if a == 'XCP': _7d_history_in_xcp = _7d_history
                else: _7d_history_in_btc = _7d_history
        else: #get the XCP/BTC market and invert for BTC/XCP (_7d_history_in_btc)
            _7d_history = mongo_db.trades.aggregate([
                {"$match": {
                    "base_asset": 'XCP',
                    "quote_asset": 'BTC',
                    "block_time": {"$gte": start_dt_7d }
                }},
                {"$project": {
                    "year":  {"$year": "$block_time"},
                    "month": {"$month": "$block_time"},
                    "day":   {"$dayOfMonth": "$block_time"},
                    "hour":  {"$hour": "$block_time"},
                    "unit_price": 1,
                    "base_quantity_normalized": 1 #to derive volume
                }},
                {"$sort": {"block_time": pymongo.ASCENDING}},
                {"$group": {
                    "_id":   {"year": "$year", "month": "$month", "day": "$day", "hour": "$hour"},
                    "price": {"$avg": "$unit_price"},
                    "vol":   {"$sum": "$base_quantity_normalized"},
                }},
            ])
            _7d_history = [] if not _7d_history['ok'] else _7d_history['result']
            _7d_history_in_xcp = _7d_history
            _7d_history_in_btc = copy.deepcopy(_7d_history_in_xcp)
            for i in xrange(len(_7d_history_in_btc)):
                _7d_history_in_btc[i]['price'] = calc_inverse(_7d_history_in_btc[i]['price'])
                _7d_history_in_btc[i]['vol'] = calc_inverse(_7d_history_in_btc[i]['vol'])
        
        for l in [_7d_history_in_xcp, _7d_history_in_btc]:
            for e in l: #convert our _id field out to be an epoch ts (in ms), and delete _id
                e['when'] = time.mktime(datetime.datetime(e['_id']['year'], e['_id']['month'], e['_id']['day'], e['_id']['hour']).timetuple()) * 1000 
                del e['_id']

        return {
            '7d_history_in_xcp': [[e['when'], e['price']] for e in _7d_history_in_xcp],
            '7d_history_in_btc': [[e['when'], e['price']] for e in _7d_history_in_btc],
        }

    if not config.CAUGHT_UP:
        logging.warn("Not updating asset market info as CAUGHT_UP is false.")
        gevent.spawn_later(COMPILE_ASSET_MARKET_INFO_PERIOD, compile_asset_market_info, mongo_db)
        return
    
    #grab the last block # we processed assets data off of
    last_block_assets_compiled = mongo_db.app_config.find_one()['last_block_assets_compiled']
    last_block_time_assets_compiled = util.get_block_time(mongo_db, last_block_assets_compiled)
    #logging.debug("Comping info for assets traded since block %i" % last_block_assets_compiled)
    current_block_index = config.CURRENT_BLOCK_INDEX #store now as it may change as we are compiling asset data :)
    current_block_time = util.get_block_time(mongo_db, current_block_index)

    if current_block_index == last_block_assets_compiled:
        #all caught up -- call again in 10 minutes
        gevent.spawn_later(COMPILE_ASSET_MARKET_INFO_PERIOD, compile_asset_market_info, mongo_db)
        return

    mps_xcp_btc, xcp_btc_price, btc_xcp_price = get_price_primatives()
    all_traded_assets = list(set(list(['BTC', 'XCP']) + list(mongo_db.trades.find({}, {'quote_asset': 1, '_id': 0}).distinct('quote_asset'))))
    
    #######################
    #get a list of all assets with a trade within the last 24h (not necessarily just against XCP and BTC)
    # ^ this is important because compiled market info has a 24h vol parameter that designates total volume for the asset across ALL pairings
    start_dt_1d = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    
    assets = list(set(
          list(mongo_db.trades.find({'block_time': {'$gte': start_dt_1d}}).distinct('quote_asset'))
        + list(mongo_db.trades.find({'block_time': {'$gte': start_dt_1d}}).distinct('base_asset'))
    ))
    for asset in assets:
        market_info_24h = compile_24h_market_info(asset)
        mongo_db.asset_market_info.update({'asset': asset}, {"$set": market_info_24h})
    #for all others (i.e. no trade in the last 24 hours), zero out the 24h trade data
    non_traded_assets = list(set(all_traded_assets) - set(assets))
    mongo_db.asset_market_info.update( {'asset': {'$in': non_traded_assets}}, {"$set": {
            '24h_summary': {'vol': 0, 'count': 0},
            '24h_ohlc_in_xcp': {},
            '24h_ohlc_in_btc': {},
            '24h_vol_price_change_in_xcp': None,
            '24h_vol_price_change_in_btc': None,
    }}, multi=True)
    logging.info("Block: %s -- Calculated 24h stats for: %s" % (current_block_index, ', '.join(assets)))
    
    #######################
    #get a list of all assets with a trade within the last 7d up against XCP and BTC
    start_dt_7d = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    assets = list(set(
          list(mongo_db.trades.find({'block_time': {'$gte': start_dt_7d}, 'base_asset': {'$in': ['XCP', 'BTC']}}).distinct('quote_asset'))
        + list(mongo_db.trades.find({'block_time': {'$gte': start_dt_7d}}).distinct('base_asset'))
    ))
    for asset in assets:
        market_info_7d = compile_7d_market_info(asset)
        mongo_db.asset_market_info.update({'asset': asset}, {"$set": market_info_7d})
    non_traded_assets = list(set(all_traded_assets) - set(assets))
    mongo_db.asset_market_info.update( {'asset': {'$in': non_traded_assets}}, {"$set": {
            '7d_history_in_xcp': [],
            '7d_history_in_btc': [],
    }}, multi=True)
    logging.info("Block: %s -- Calculated 7d stats for: %s" % (current_block_index, ', '.join(assets)))

    #######################
    #update summary market data for assets traded since last_block_assets_compiled
    #get assets that were traded since the last check with either BTC or XCP, and update their market summary data
    assets = list(set(
          list(mongo_db.trades.find({'block_index': {'$gt': last_block_assets_compiled}, 'base_asset': {'$in': ['XCP', 'BTC']}}).distinct('quote_asset'))
        + list(mongo_db.trades.find({'block_index': {'$gt': last_block_assets_compiled}}).distinct('base_asset'))
    ))
    #update our storage of the latest market info in mongo
    for asset in assets:
        logging.info("Block: %s -- Updating asset market info for %s ..." % (current_block_index, asset))
        summary_info = compile_summary_market_info(asset, mps_xcp_btc, xcp_btc_price, btc_xcp_price)
        mongo_db.asset_market_info.update( {'asset': asset}, {"$set": summary_info}, upsert=True)

    
    #######################
    #next, compile market cap historicals (and get the market price data that we can use to update assets with new trades)
    #NOTE: this algoritm still needs to be fleshed out some...I'm not convinced it's laid out/optimized like it should be
    #start by getting all trades from when we last compiled this data
    trades = mongo_db.trades.find({'block_index': {'$gt': last_block_assets_compiled}}).sort('block_index', pymongo.ASCENDING)
    trades_by_block = [] #tracks assets compiled per block, as we only want to analyze any given asset once per block
    trades_by_block_mapping = {} 
    #organize trades by block
    for t in trades:
        if t['block_index'] in trades_by_block_mapping:
            assert trades_by_block_mapping[t['block_index']]['block_index'] == t['block_index']
            assert trades_by_block_mapping[t['block_index']]['block_time'] == t['block_time']
            trades_by_block_mapping[t['block_index']]['trades'].append(t)
        else:
            e = {'block_index': t['block_index'], 'block_time': t['block_time'], 'trades': [t,]}
            trades_by_block.append(e)
            trades_by_block_mapping[t['block_index']] = e  

    for t_block in trades_by_block:
        #reverse the tradelist per block, and ensure that we only process an asset that hasn't already been processed for this block
        # (as there could be multiple trades in a single block for any specific asset). we reverse the list because
        # we'd rather process a later trade for a given asset, as the market price for that will take into account
        # the earlier trades on that same block for that asset, and we don't want/need multiple cap points per block
        assets_in_block = {}
        mps_xcp_btc, xcp_btc_price, btc_xcp_price = get_price_primatives(end_dt=t_block['block_time'])
        for t in reversed(t_block['trades']):
            assets = []
            if t['base_asset'] not in assets_in_block:
                assets.append(t['base_asset'])
                assets_in_block[t['base_asset']] = True
            if t['quote_asset'] not in assets_in_block:
                assets.append(t['quote_asset'])
                assets_in_block[t['quote_asset']] = True
            if not len(assets): continue
    
            for asset in assets:
                #recalculate the market cap for the asset this trade is for
                asset_info = get_asset_info(asset, at_dt=t['block_time'])
                (price_summary_in_xcp, price_summary_in_btc, price_in_xcp, price_in_btc, aggregated_price_in_xcp, aggregated_price_in_btc
                ) = get_xcp_btc_price_info(asset, mps_xcp_btc, xcp_btc_price, btc_xcp_price, with_last_trades=0, end_dt=t['block_time'])
                market_cap_in_xcp, market_cap_in_btc = calc_market_cap(asset_info, price_in_xcp, price_in_btc)
                #^ this will get price data from the block time of this trade back the standard number of days and trades
                # to determine our standard market price, relative (anchored) to the time of this trade
        
                for market_cap_as in ('XCP', 'BTC'):
                    market_cap = market_cap_in_xcp if market_cap_as == 'XCP' else market_cap_in_btc
                    #if there is a previously stored market cap for this asset, add a new history point only if the two caps differ
                    prev_market_cap_history = mongo_db.asset_marketcap_history.find({'market_cap_as': market_cap_as, 'asset': asset,
                        'block_index': {'$lt': t['block_index']}}).sort('block_index', pymongo.DESCENDING).limit(1)
                    prev_market_cap_history = list(prev_market_cap_history)[0] if prev_market_cap_history.count() == 1 else None
                    
                    if market_cap and (not prev_market_cap_history or prev_market_cap_history['market_cap'] != market_cap):
                        mongo_db.asset_marketcap_history.insert({
                            'block_index': t['block_index'],
                            'block_time': t['block_time'],
                            'asset': asset,
                            'market_cap': market_cap,
                            'market_cap_as': market_cap_as,
                        })
                        logging.info("Block %i -- Calculated market cap history point for %s as %s (mID: %s)" % (t['block_index'], asset, market_cap_as, t['message_index']))

    #all done for this run...call again in a bit                            
    gevent.spawn_later(COMPILE_ASSET_MARKET_INFO_PERIOD, compile_asset_market_info, mongo_db)
    mongo_db.app_config.update({}, {'$set': {'last_block_assets_compiled': current_block_index}})
    