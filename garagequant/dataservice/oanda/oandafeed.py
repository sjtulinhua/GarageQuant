# encoding: UTF-8

"""

Data service

"""

import time
import os
import logging
# import datetime
import contextlib
import pandas as pd
# import numpy as np
# import tables as tb

# import oanda api
from oandapyV20 import API
from oandapyV20.contrib.factories import InstrumentsCandlesFactory

# import in-house modules
from .oanda import Oanda
from garagequant.utils import gqutil
from garagequant.dataservice import dataservice as gqds

""" HDF5_COMP_LEVEL：0~9 """
HDF5_COMP_LEVEL = 4
""" HDF5_COMP_LIB: 'blosc', 'bzip2', 'lzo', 'zlib'"""
HDF5_COMP_LIB = 'blosc'
""" Write chunk (in candle rows)"""
HDFSTORE_CHUNK_IN_ROW: int = 50  #2000000

logger = logging.getLogger(__name__)


class OandaApiParam:
    def __init__(self, data_spec):
        self.param = {
            'price': data_spec['price'],
            'from': data_spec['startdate'],
            'to': data_spec['enddate'],
            'count': data_spec['barcound']
        }

    def set_param_field(self, field, value):
        self.param[field] = value


class OandaDataService(gqds.DataService):

    def __init__(self, oanda_config=None):
        try:
            Oanda.oanda_initialize(oanda_config)
        except Exception as e:
            logger.exception(f'OandaDataService instance init error:\n{str(Exception)} - {str(e)}')

        self._oanda_client = None
        self._fetch_stats = []
        self._storage = None  # general storage object reference to specified data storage
        self._chunk_feed = None

    def ask_for_data(self):
        """
        Asking instrument data for algorithms (not heart beat)
        """
        pass

    def get_backtest_data_feed(self, backtestconfig):
        """
        Generate backtest heartbeat provider
        :return: Generator for feeding data
        """
        # parse path name of target data files from config
        storage_type = backtestconfig['storage']
        storage_spec = backtestconfig['storage_spec']
        path_name = ''
        start_date = ''
        end_date = ''
        group = None

        if storage_type == 'hdf5':
            file_path = os.path.expanduser(storage_spec['hdf5']['file_path'])
            file_name = str.lower(f"{backtestconfig['instrument']}_{Oanda.account_type}.hdf5")
            path_name = os.path.join(file_path, file_name)
            start_date = backtestconfig['startdate']
            end_date = backtestconfig['enddate']
            group = backtestconfig['granularity']
        else:  # for future supported storages (csv, database, etc.)
            raise NotImplementedError

        def bar_generator():
            bar_feed = None
            bar_cnt = 0

            if bar_feed is None:
                bar_feed = self._get_bar_feed(path_name, storage_type, start_date, end_date,
                                              group=group, chunksize=HDFSTORE_CHUNK_IN_ROW)
            while True:
                try:
                    ret = next(bar_feed)
                    yield ret
                    bar_cnt += 1
                except StopIteration:
                    bar_feed = self._get_bar_feed(path_name, storage_type, start_date, end_date,
                                                  group=group, chunksize=HDFSTORE_CHUNK_IN_ROW)
                    if bar_feed is not None:
                        continue
                    else:
                        logger.info(f'In _get_bar_feed -> bar_generator, all the bars (total: {bar_cnt}) loaded ')
                        break
                except Exception as e:
                    logger.exception(f'In _get_bar_feed -> bar_generator error:\n{str(Exception)} - {str(e)}')
                    break

        feed = bar_generator()
        return feed

    def _get_bar_feed(self, file_path_name, storage_type, start_date, end_date, group=None, chunksize=None):
        if storage_type == 'hdf5':
            if self._storage is None:
                self._chunk_feed = \
                    self._get_chunk_feed(file_path_name, storage_type, start_date, end_date, group=group,
                                         chunksize=chunksize)
            try:
                df_bar_chunk = next(self._chunk_feed)
                logger.debug(f'load hdf5 chunk: {df_bar_chunk.shape[0]} bars')
                bar_feed = df_bar_chunk.iterrows()
            except StopIteration:
                logger.info(f'all hdf5 chunks loaded')
                self._storage.close()
                return None
            else:
                return bar_feed

        else:  # for future supported storages (csv, database, etc.)
            raise NotImplementedError

    @gqutil.measuretime
    def _get_chunk_feed(self, file_path_name, storage_type, start_date, end_date, group=None, chunksize=None):
        if storage_type == 'hdf5':
            try:
                if self._storage is None:
                    self._storage = pd.HDFStore(file_path_name, 'r')
                    where = 'timestamp >= start_date and timestamp <= end_date'
                    feed = self._storage.select(group, where=where, iterator=True, chunksize=chunksize)
                else:
                    return
            except Exception as e:
                logger.exception(f'_get_chunk_feed error:\n{str(Exception)} - {str(e)}')
                self._storage.close()
                raise
            else:
                return iter(feed)  # return a chunk of bars in every iteration

        else:  # for future supported storages (csv, database, etc.)
            raise NotImplementedError

    def get_storage_ref(self):
        return self._storage

    def live_data_feed(self):
        pass

    def fetch_data(self, fetch_config):
        """
        download data from oanda server following data_config rules
        """
        try:
            storage = fetch_config['storage']
            if storage == 'hdf5':
                self._fetch_to_hdf5(fetch_config['storage_spec'], fetch_config['data_spec'])

        except Exception as e:
            logger.exception(f'load historical data error:\n{str(Exception)} - {str(e)}')

        self._dump_fetch_stats()

    def _dump_fetch_stats(self):
        """
        dump download stats
        :return: None
        """
        if self._fetch_stats:
            for fetch_stats_dict in self._fetch_stats:
                for file_name, group_list in fetch_stats_dict.items():
                    logger.info(str(file_name))
                    for stats_dict in group_list:
                        for key, val in stats_dict.items():
                            if key == 'group':
                                logger.info(f'\t{key}: {val}')
                            else:
                                logger.info(f'\t\t{key}: {val}')

    def _fetch_to_hdf5(self, storage_spec, data_spec):
        """
        parse the list of target hdf5 files with fetch params from data_spec section of trade configuration
        """
        fetch_list = []

        # each hdf5 file accommodates on instrument, each period is stored in separate group
        for inst in data_spec['instruments']:
            api_param_list = []

            # ensure the data directory exists
            file_path = os.path.expanduser(storage_spec['hdf5']['file_path'])
            if not os.path.isdir(file_path):
                gqutil.check_data_dir(file_path)

            file_name = str.lower(f'{inst}_{Oanda.account_type}.hdf5')
            path_name = os.path.join(file_path, file_name)

            for granularity in data_spec['periods']:
                api_param = OandaApiParam(data_spec)
                api_param.set_param_field('granularity', granularity)
                api_param_list.append(api_param)

            fetch_list.append((path_name, inst, api_param_list))

        self._download_data_to_hdf5(fetch_list, storage_spec['tableheader'])

    def _download_data_to_hdf5(self, download_list, field_dtype_dict):
        # create Oanda session
        if self._oanda_client is None:
            self._create_client_session()

        # download and save to files
        for path_name, inst, param_list in download_list:

            # each hdf5 file accommodates on instrument, each period is stored in separate group
            with pd.HDFStore(path_name, 'a', complevel=HDF5_COMP_LEVEL, complib=HDF5_COMP_LIB) as h5f:
                # todo avid downloading existed data:
                #       1. label existed data by unique key which combining volume and start/to dates
                #       2. could assume already existed data is correct (continuously, no missed data in it)
                #       3. add unique key as an attr of a h5 table so that can used for judge new data
                #          whether already existed or not

                def _dump_h5(store, cache, final=True):
                    # shift to appropriate data type for shrinking data size
                    for col in cache.columns:
                        cache[col] = cache[col].astype(field_dtype_dict[col], copy=True)
                        logger.debug(cache[col].dtypes)

                    # reset index
                    # df_candles.set_index('timestamp', inplace=True)

                    if final:
                        logger.info('\n\t\t **** Dumping to file: completed ****\n')
                    else:
                        logger.info('\n\t\t **** Dumping to file: to continue (in loop) ****\n')

                    store.append(api_param.param['granularity'], cache, data_columns=True)

                logger.info(f'download to file: {os.path.abspath(path_name)}')

                stats = {f'{path_name}': [], }

                child_stats = stats[f'{path_name}']

                for api_param in param_list:
                    start_time = time.time()

                    requests = 0
                    candle_cnt = 0
                    stats_dict = {'group': None, 'num of candles': 0, }
                    df_cache = None

                    # for simplify: remove existed data
                    try:
                        logger.info(f"About to override data in Group {api_param.param['granularity']} ")
                        h5f.remove(api_param.param['granularity'])
                    except KeyError:
                        logger.info(f"Group {api_param.param['granularity']} doesn't exist, create & write")

                    # The factory returns a generator generating consecutive
                    # requests to retrieve full history from date 'from' till 'to'
                    for r in InstrumentsCandlesFactory(instrument=inst, params=api_param.param):
                        self._oanda_client.request(r)
                        candles = r.response.get('candles')  # candles is a list
                        requests += 1

                        if not candles:
                            logger.info(f'skip to write next: find empty data with candles == []')
                            continue
                        else:
                            candle_cnt += len(candles)
                            stats_dict['num of candles'] = candle_cnt
                            logger.info(f'* download progress: {candles[0].get("time")} < {len(candles)} candles>')

                        candles = list(map(self._normalize_oanda_raw_candles, candles))

                        if df_cache is None:
                            df_cache = pd.DataFrame(candles)
                        else:
                            # df_cache = df_cache.append(candles)
                            next_cached_idx = df_cache.shape[0]
                            new_idx = range(next_cached_idx, next_cached_idx + len(candles))
                            df_cache = df_cache.append(pd.DataFrame(candles, index=new_idx))

                        cache_size = df_cache.memory_usage(deep=True).sum()
                        logger.info(f"  cached {candle_cnt} candles have used {cache_size} Bytes, \
                                                        {cache_size / candle_cnt}B/Candle")

                        if candle_cnt > HDFSTORE_CHUNK_IN_ROW:
                            assert (df_cache.shape[0] == candle_cnt)
                            candle_cnt = 0
                            _dump_h5(h5f, df_cache, final=False)
                            df_cache = None

                    if df_cache is not None:
                        _dump_h5(h5f, df_cache, final=True)
                    else:
                        logger.info(f'candles are fetched inside the loop')

                    h5f.create_table_index(api_param.param['granularity'], columns=True, optlevel=9, kind='full')
                    end_time = time.time()

                    stats_dict['group'] = api_param.param['granularity']
                    stats_dict['request count'] = requests
                    stats_dict['download time'] = end_time - start_time
                    # logger.info('\t - it took {} second to download {} - {} '
                    #                .format(stats_dict['download time'], inst, stats_dict['group']))
                    child_stats.append(stats_dict)

                self._fetch_stats.append(stats)

    @staticmethod
    def _normalize_oanda_raw_candles(oanda_candles):
        from collections import OrderedDict
        ohlc = OrderedDict(timestamp=pd.to_datetime(oanda_candles['time']),
                           openbid=float(oanda_candles['bid']['o']),
                           highbid=float(oanda_candles['bid']['h']),
                           lowbid=float(oanda_candles['bid']['l']),
                           closebid=float(oanda_candles['bid']['c']),
                           openask=float(oanda_candles['ask']['o']),
                           highask=float(oanda_candles['ask']['h']),
                           lowask=float(oanda_candles['ask']['l']),
                           closeask=float(oanda_candles['ask']['c']),
                           volume=float(oanda_candles['volume']),
                           complete=float(oanda_candles['complete']))
        return ohlc

    def _create_client_session(self):
        self._oanda_client = API(access_token=Oanda.token, environment=Oanda.account_type)


def fetch_oanda_data(oandaconfig, fetchconfig):
    """
    for action == getdata, download data from oanda server
    #todo for simplifying, data would be override, improve this!
    """
    ods = OandaDataService(oandaconfig)
    ods.fetch_data(fetchconfig)
