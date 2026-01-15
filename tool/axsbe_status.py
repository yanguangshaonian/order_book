# -*- coding: utf-8 -*-

import tool.axsbe_base as axsbe_base
from tool.axsbe_base import TPI, TPM
import struct


class axsbe_status(axsbe_base.axsbe_base):
    '''
    深交所:心跳
    上交所:心跳、债券逐笔合并流市场状态
    '''
    
    __slots__ = [
        'SecurityIDSource',
        'MsgType',
        'SecurityID',
        'ChannelNo',
        'ApplSeqNum',
        'TradingPhaseInstrument',
    ]
    
    def __init__(self, SecurityIDSource=axsbe_base.SecurityIDSource_NULL, MsgType=axsbe_base.MsgType_heartbeat):
        super(axsbe_status, self).__init__(MsgType, SecurityIDSource)
        self.TradingPhaseInstrument = 0

    def load_dict(self, dict:dict):
        '''从字典加载字段'''
        #公共头
        self.SecurityIDSource = dict['SecurityIDSource']
        self.MsgType = dict['MsgType']
        self.SecurityID = dict['SecurityID']
        self.ChannelNo = dict['ChannelNo']
        self.ApplSeqNum = dict['ApplSeqNum']
        
        #消息体
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            pass
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            self.TradingPhaseInstrument = dict['TradingPhase']
            if self.MsgType==axsbe_base.MsgType_heartbeat:
                pass
            elif self.MsgType==axsbe_base.MsgType_status_sse_bond:
                self.TradingPhaseInstrument = dict['TradingPhase']
            else:
                raise Exception(f'Not support SSE status Type={self.MsgType}')
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    @property
    def TradingPhaseMarket(self):
        '''
        市场交易阶段：
        '''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            return TPM.Unknown
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.MsgType==axsbe_base.MsgType_heartbeat:
                return TPM.Unknown
            elif self.MsgType==axsbe_base.MsgType_status_sse_bond:
                if self.TradingPhaseInstrument==0:
                    return TPM.Starting
                if self.TradingPhaseInstrument==1:
                    return TPM.OpenCall
                elif self.TradingPhaseInstrument==2:
                    return TPM.ContinuousAutomaticMatching
                elif self.TradingPhaseInstrument==6:
                    return TPM.HangingUp
                elif self.TradingPhaseInstrument==5:
                    return TPM.Closing
                elif self.TradingPhaseInstrument==12:
                    return TPM.Ending
                elif self.TradingPhaseInstrument==11:
                    return TPM.OffMarket
                else:
                    raise Exception(f'Unknown SSE status TP={self.TradingPhaseInstrument}')
            else:
                raise Exception(f'Not support SSE status Type={self.MsgType}')
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    def __str__(self):
        '''打印log，只有合法的SecurityIDSource才能被打印'''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            return f'心跳, Seq={self.ApplSeqNum}'
        elif  self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.MsgType==axsbe_base.MsgType_heartbeat:
                return f'心跳, Seq={self.ApplSeqNum}'
            elif self.MsgType==axsbe_base.MsgType_status_sse_bond:
                return f'{"%06d"%self.SecurityID} TP={self.TradingPhase_str}'
            else:
                raise Exception(f'Not support SSE status Type={self.MsgType}')
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    @property
    def bytes_stream(self):
        return

    def unpack_stream(self, bytes_i:bytes):
        return
    
    @property
    def ccode(self):
        return
        

    def save(self):
        return

    def load(self, data):
        return