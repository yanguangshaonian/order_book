# -*- coding: utf-8 -*-

import tool.axsbe_base as axsbe_base
import  struct

class axsbe_exe(axsbe_base.axsbe_base):
    '''
    深交所:成交或撤单
    上交所:成交
    '''
    __slots__ = [
        'SecurityIDSource',
        'MsgType',
        'SecurityID',
        'ChannelNo',
        'ApplSeqNum',
        'TransactTime',     # SH-STOCK.TradeTime;   SH-BOND.TickTime

        'BidApplSeqNum',    # SH-STOCK.TradeBuyNo;  SH-BOND.BuyOrderNo
        'OfferApplSeqNum',  # SH-STOCK.TradeSellNo; SH-BOND.SellOrderNo
        'LastPx',           # SH-STOCK.LastPx;      SH-BOND.Price
        'LastQty',          # SH-STOCK.LastQty;     SH-BOND.Qty
        'ExecType',         # 对于深圳代表的 是否是撤单 或者成交,  对于上海 就是 内盘还是外盘

        'BizIndex',         #SH-STOCK  上海专属

        'TradeMoney',       #SH-BOND
    ]
    
    def __init__(self, SecurityIDSource=axsbe_base.SecurityIDSource_NULL, MsgType=axsbe_base.MsgType_exe_stock):
        super(axsbe_exe, self).__init__(MsgType, SecurityIDSource)
        self.BidApplSeqNum = 0xffffffffffffffff      # 通道内 买 序号
        self.OfferApplSeqNum = 0xffffffffffffffff    # 通道内 卖 序号
        self.LastPx = 0
        self.LastQty = 0
        self.ExecType = 0
        self.TransactTime = 0

        self.BizIndex = 0 # 上交所专属 消息序号  仅上海有效，有效时从1开始

        self.TradeMoney = 0

    def load_dict(self, dict:dict):
        '''从字典加载字段'''
        #公共头
        self.SecurityIDSource = dict['SecurityIDSource']
        self.SecurityID = dict['SecurityID']
        self.ChannelNo = dict['ChannelNo']
        self.ApplSeqNum = dict['ApplSeqNum']

        #消息体
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            self.BidApplSeqNum = dict['BidApplSeqNum']
            self.OfferApplSeqNum = dict['OfferApplSeqNum']
            self.LastPx = dict['LastPx']
            self.LastQty = dict['LastQty']
            self.ExecType = dict['ExecType']
            self.TransactTime = dict['TransactTime']

        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.MsgType==axsbe_base.MsgType_exe_stock:
                self.BidApplSeqNum = dict['BidApplSeqNum']
                self.OfferApplSeqNum = dict['OfferApplSeqNum']
                self.LastPx = dict['LastPx']
                self.LastQty = dict['LastQty']
                self.ExecType = dict['ExecType']
                self.TransactTime = dict['TransactTime']

                self.BizIndex = dict['BizIndex']
            elif self.MsgType==axsbe_base.MsgType_exe_sse_bond:
                self.ExecType = dict['TradingPhase']
                self.BidApplSeqNum = dict['BuyOrderNo']
                self.OfferApplSeqNum = dict['SellOrderNo']
                self.LastPx = dict['Price']
                self.LastQty = dict['Qty']
                self.TradeMoney = dict['TradeMoney']
                self.TransactTime = dict['TickTime']
            else:
                raise Exception(f'Not support SSE exec Type={self.MsgType}')
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    def is_same(self, another):
        '''用于比较模拟撮合和历史数据是否一致'''
        SecurityID_isSame = self.SecurityID == another.SecurityID

        BidApplSeqNum_isSame = self.BidApplSeqNum == another.BidApplSeqNum
        OfferApplSeqNum_isSame = self.OfferApplSeqNum == another.OfferApplSeqNum

        LastPx_isSame = self.LastPx == another.LastPx
        LastQty_isSame = self.LastQty == another.LastQty
        ExecType_isSame = self.ExecType == another.ExecType

        BizIndex_isSame = self.BizIndex == another.BizIndex

        TradeMoney_isSame = self.TradeMoney == another.TradeMoney
        if SecurityID_isSame \
            and BidApplSeqNum_isSame \
            and OfferApplSeqNum_isSame \
            and LastPx_isSame \
            and LastQty_isSame \
            and ExecType_isSame \
            and BizIndex_isSame \
            and TradeMoney_isSame:
            return True
        else:
            return False

    @property
    def ExecType_str(self):
        '''打印执行类型'''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            if self.ExecType==ord('F'):
                return '成交'
            elif self.ExecType==ord('4'):
                return '撤单'
            raise RuntimeError(f"非法执行类型:{self.ExecType}")
        elif  self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE: # 内外盘标志: 'B'=外盘，主动买; 'S'=内盘，主动卖; 'N'=未知
            if self.ExecType==ord('B'):
                return '外盘'
            elif self.ExecType==ord('S'):
                return '内盘'
            elif self.ExecType==ord('N'):
                return '未知'
            raise RuntimeError(f"非法 TradeBSFlag={self.ExecType}")
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    def __str__(self):
        '''打印log，只有合法的SecurityIDSource才能被打印'''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            return f'{"%06d"%self.SecurityID} T={self.ExecType_str}, Px={self.LastPx}, Qty={self.LastQty}, Seq={self.ApplSeqNum}, BidSeq={self.BidApplSeqNum}, AskSeq={self.OfferApplSeqNum}, @{self.TransactTime}'
        elif  self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.MsgType==axsbe_base.MsgType_exe_stock:
                return f'{"%06d"%self.SecurityID} T={self.ExecType_str}, Px={self.LastPx}, Qty={self.LastQty}, Seq={self.ApplSeqNum}, BidSeq={self.BidApplSeqNum}, AskSeq={self.OfferApplSeqNum}, BizIndex={self.BizIndex}, @{self.TransactTime}'
            elif self.MsgType==axsbe_base.MsgType_exe_sse_bond:
                return f'{"%06d"%self.SecurityID} T={self.ExecType_str}, Px={self.LastPx}, Qty={self.LastQty}, Seq={self.ApplSeqNum}, BidSeq={self.BidApplSeqNum}, AskSeq={self.OfferApplSeqNum}, @{self.TransactTime}'
            else:
                raise Exception(f'Not support SSE exec Type={self.MsgType}')
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
