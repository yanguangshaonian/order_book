# -*- coding: utf-8 -*-

from datetime import datetime
from time import sleep
from tool.axsbe_base import SecurityIDSource_SZSE, TPM, INSTRUMENT_TYPE
from tool.test_util import *
from tool.msg_util import *
from behave.mu import *
import os
import pickle

def print_log(h, msg):
    print(msg)
    if h!=print:
        h(msg)

def TEST_axob_SL(date, instrument:int, 
                SecurityIDSource=SecurityIDSource_SZSE, 
                instrument_type=INSTRUMENT_TYPE.STOCK
                ):
    '''TODO: axob save/load'''
    n_max=31500
    n_save = 800

    md_file = f'data/{date}/AX_sbe_szse_{instrument:06d}.log'
    if not os.path.exists(md_file):
        raise f"{md_file} not exists"

    axob_save = AXOB(instrument, SecurityIDSource, instrument_type)
    save_data = None

    n = 0
    boc = 0
    for msg in axsbe_file(md_file):
        if msg.TradingPhaseMarket==TPM.OpenCall and boc==0:
            boc = 1
            print('openCall start')

        if msg.TradingPhaseMarket>TPM.OpenCall:
            print(f'openCall over, n={n}')
            break
        axob_save.onMsg(msg)
        n += 1
        if n==n_save:
            save_data = axob_save.save()
            print(f'save at {n}')
        if n>=n_max:
            print(f'nb over, n={n} saved')
            break
    axob_save.are_you_ok()

    pickle.dump(save_data,open("log/test.pkl",'wb'))
    load_data = pickle.load(open("log/test.pkl",'rb'))

    axob_load = AXOB(instrument, SecurityIDSource, instrument_type)
    axob_load.load(load_data)
    n = 0
    for msg in axsbe_file(md_file):
        if msg.TradingPhaseMarket>TPM.OpenCall:
            print(f'openCall over, n={n}')
            break
        n += 1
        if n==n_save:
            print(f'load at {n}')
        if n>n_save:
            axob_load.onMsg(msg)
        if n>=n_max:
            print(f'nb over, n={n} saved')
            break
    axob_load.are_you_ok()

    last_snap_save = axob_save.last_snap
    last_snap_load = axob_load.last_snap
    assert str(last_snap_save)==str(last_snap_load)

    print(f'axob_save.rebuilt_snaps={len(axob_save.rebuilt_snaps)}; axob_load.rebuilt_snaps={len(axob_load.rebuilt_snaps)}')
    assert len(axob_save.rebuilt_snaps)==len(axob_load.rebuilt_snaps)
    print(f'axob_save.market_snaps={len(axob_save.market_snaps)}; axob_load.market_snaps={len(axob_load.market_snaps)}')
    assert len(axob_save.market_snaps)==len(axob_load.market_snaps)
    print(f'axob_save.rebuilt_snaps={len(axob_save.rebuilt_snaps)}; axob_load.rebuilt_snaps={len(axob_load.rebuilt_snaps)}')
    assert len(axob_save.order_map)==len(axob_load.order_map)
    assert len(axob_save.bid_level_tree)==len(axob_load.bid_level_tree)
    assert len(axob_save.ask_level_tree)==len(axob_load.ask_level_tree)

    f_data_save = axob_save.save()
    pickle.dump(f_data_save,open("log/f_data_save.pkl",'wb'))

    f_data_load = axob_load.save()
    pickle.dump(f_data_load,open("log/f_data_load.pkl",'wb'))
    #.pkl should be binary-same.

    # with open('log/f_data_save.json', 'w') as f: # not real json
    #     f.write(str(f_data_save))

    # with open('log/f_data_load.json', 'w') as f: # not real json
    #     f.write(str(f_data_load))
    assert str(f_data_save)==str(f_data_load)

    print("TEST_axob_SL done")
    return


def TEST_mu_SL(source_file, instrument_list, 
                SecurityIDSource=SecurityIDSource_SZSE, 
                instrument_type=INSTRUMENT_TYPE.STOCK
                ):
    '''TODO: axob save/load'''
    msg_n_max=315
    msg_n_save = 80

    if not os.path.exists(source_file):
        raise f"{source_file} not exists"

    mu_save = MU(instrument_list, SecurityIDSource, instrument_type)
    save_data = None

    n = 0
    boc = 0
    saved =0
    for msg in axsbe_file(source_file):
        if msg.TradingPhaseMarket==TPM.OpenCall and boc==0:
            boc = 1
            print('openCall start')

        if msg.TradingPhaseMarket>TPM.OpenCall:
            print(f'openCall over, n={n}, mu.msg_nb={mu_save.msg_nb}')
            break
        mu_save.onMsg(msg)
        n += 1
        if mu_save.msg_nb==msg_n_save and saved==0:
            save_data = mu_save.save()
            saved = 1
            n_save = n
            print(f'save at {n}')
        if mu_save.msg_nb>=msg_n_max:
            print(f'nb over, n={n} saved')
            n_max = n
            break
    mu_save.are_you_ok()

    pickle.dump(save_data,open("log/test.pkl",'wb'))
    load_data = pickle.load(open("log/test.pkl",'rb'))

    mu_load = MU(instrument_list, SecurityIDSource, instrument_type, load_data=load_data)

    n = 0
    for msg in axsbe_file(source_file):
        if msg.TradingPhaseMarket>TPM.OpenCall:
            print(f'openCall over, n={n}')
            break
        n += 1
        if n==n_save:
            print(f'load at {n}')
        if n>n_save:
            mu_load.onMsg(msg)
        if n>=n_max:
            print(f'nb over, n={n} saved')
            break
    mu_load.are_you_ok()

    f_data_save = mu_save.save()
    pickle.dump(f_data_save,open("log/f_data_save.pkl",'wb'))

    f_data_load = mu_load.save()
    pickle.dump(f_data_load,open("log/f_data_load.pkl",'wb'))
    #.pkl should be binary-same.

    # with open('log/f_data_save.json', 'w') as f: # not real json
    #     f.write(str(f_data_save))

    # with open('log/f_data_load.json', 'w') as f: # not real json
    #     f.write(str(f_data_load))
    assert str(f_data_save)==str(f_data_load)

    print("TEST_mu_SL done")
    return


def TEST_axob_core(loader_itor, instrument_list:list, n_max=500, 
                    openCall_only=False,
                    SecurityIDSource=SecurityIDSource_SZSE, 
                    instrument_type=INSTRUMENT_TYPE.STOCK,
                    HHMMSSms_max=None,
                    logPack=(print, print, print, print)
                ):
    DBG, INFO, WARN, ERR = logPack

    mu = MU(instrument_list, SecurityIDSource, instrument_type)
    print_log(INFO, f'{datetime.today()} instrumen_nb={len(instrument_list)}, current memory usage={getMemUsageGB():.3f} GB')

    n = 0 #只计算在 instrument_list 内的消息
    n_bgn = 0
    boc = 0
    ecc = 0
    t_bgn = time()
    t_pf = t_bgn
    profile_memUsage = 0
    profile_memFree = getMemFreeGB()
    for msg in loader_itor:
        if msg.TradingPhaseMarket==TPM.OpenCall and boc==0:
            boc = 1
            print_log(INFO, f'{datetime.today()} openCall start')

        if msg.TradingPhaseMarket==TPM.Ending and ecc==0:
            ecc = 1
            print_log(INFO, f'{datetime.today()} closeCall over')

        mu.onMsg(msg)
        n += 1
        if n_max>0 and n>=n_max:
            print_log(INFO, f'{datetime.today()} nb over, n={n}')
            break

        if (openCall_only and msg.HHMMSSms>92600000) or \
           (msg.HHMMSSms>150100000):
            print_log(INFO, f'{datetime.today()} Ending: over, n={n}')
            break

        if HHMMSSms_max is not None and HHMMSSms_max>0 and msg.HHMMSSms>HHMMSSms_max:
            print_log(INFO, f'{datetime.today()} HHMMSSms_max: over, n={n}, msg @({msg.TransactTime})')
            break

        now = time()

        if now > t_pf+30: #内存占用采样周期30s
            memUsage = getMemUsageGB()
            if memUsage>profile_memUsage:
                profile_memUsage = memUsage
            memFree = getMemFreeGB()
            if memFree<profile_memFree:
                profile_memFree = memFree
            t_pf = now

            if now>t_bgn+60*10:#内存情况，报告周期10min
                print_log(INFO, f'{datetime.today()} current memory usage={memUsage:.3f} GB free={memFree:.3f} GB'
                    f'(epoch peak={profile_memUsage:.3f} GB, minFree={profile_memFree:.3f} GB),' 
                    f' @{msg.HHMMSSms}')
                t_bgn = now
                profile_memUsage = 0
                profile_memFree = memFree

    if WARN is not None:
        WARN(mu) #保证能记录到文件中
    assert mu.are_you_ok()
    print_log(INFO, f'== TEST_axob_bat PASS ==')
    return

def TEST_axob_bat(source_file, instrument_list:list, n_max=500, 
                    openCall_only=False,
                    SecurityIDSource=SecurityIDSource_SZSE, 
                    instrument_type=INSTRUMENT_TYPE.STOCK,
                    HHMMSSms_max=None,
                    logPack=(print, print, print, print)
                ):
    if not os.path.exists(source_file):
        raise f"{source_file} not exists"

    # 解析文件出来
    loader_itor = axsbe_file(source_file)

    TEST_axob_core(loader_itor, 
                    instrument_list, 
                    n_max=n_max,
                    openCall_only=openCall_only,
                    SecurityIDSource=SecurityIDSource,
                    instrument_type=instrument_type,
                    HHMMSSms_max=HHMMSSms_max,
                    logPack=logPack
    )
    return


def TEST_axob(date, instrument:int, n_max=0, 
                openCall_only=False,
                SecurityIDSource=SecurityIDSource_SZSE, 
                instrument_type=INSTRUMENT_TYPE.STOCK,
                logPack=(print, print, print, print)
            ):
    if SecurityIDSource==SecurityIDSource_SZSE:
        SecurityIDSource_char = 'szse'
    elif SecurityIDSource==SecurityIDSource_SSE:
        SecurityIDSource_char = 'sse'
    md_file = f'data/{date}/AX_sbe_{SecurityIDSource_char}_{instrument:06d}.log'
    if not os.path.exists(md_file):
        raise f"{md_file} not exists"

    TEST_axob_bat(md_file, [instrument], n_max, openCall_only, SecurityIDSource, instrument_type, logPack=logPack)

    return

def TEST_axob_csv(date, instrument:int, n_max=0, 
                openCall_only=False,
                SecurityIDSource=SecurityIDSource_SZSE, 
                instrument_type=INSTRUMENT_TYPE.STOCK,
                logPack=(print, print, print, print)
            ):
    if SecurityIDSource==SecurityIDSource_SZSE:
        SecurityIDSource_char = 'SZ'
    elif SecurityIDSource==SecurityIDSource_SSE:
        SecurityIDSource_char = 'SH'
    cj_file = f'data/{date}/{instrument:06d}.{SecurityIDSource_char}.cj'
    wt_file = f'data/{date}/{instrument:06d}.{SecurityIDSource_char}.wt'
    snap_file = f'data/{date}/{instrument:06d}.{SecurityIDSource_char}.snap' #需要手动写一个快照，用于初始化AXOB

    if not os.path.exists(wt_file):
        raise f"{wt_file} not exists"
    if not os.path.exists(cj_file):
        raise f"{cj_file} not exists"
    if not os.path.exists(snap_file):
        raise f"{snap_file} not exists"

    loader_itor = axsbe_file_csv(wt_file, cj_file, snap_file)

    TEST_axob_core(loader_itor, 
                    [instrument], 
                    n_max=n_max,
                    openCall_only=openCall_only,
                    SecurityIDSource=SecurityIDSource,
                    instrument_type=instrument_type,
                    logPack=logPack
    )

    return


