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


def TEST_axob_core(loader_itor, instrument_list:list, n_max=500, 
                    openCall_only=False,
                    SecurityIDSource=SecurityIDSource_SZSE, 
                    instrument_type=INSTRUMENT_TYPE.STOCK,
                    HHMMSSms_max=None,
                    logPack=(print, print, print, print)
                ):
    DBG, INFO, WARN, ERR = logPack

    # instrument_list 是订阅的股票列表, 传入受到MU管理
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
        # 集合竞价开始和收盘 打印日志用的
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

