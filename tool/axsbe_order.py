# -*- coding: utf-8 -*-

import tool.axsbe_base as axsbe_base
import struct


class axsbe_order(axsbe_base.axsbe_base):
    '''
    深交所:新增委托
    上交所:新增委托或撤单
    '''
    
    __slots__ = [
        'SecurityIDSource',   # 所属交易所
        'MsgType',
        'SecurityID',
        'ChannelNo',    # 通道号
        'ApplSeqNum',   # 通道内的消息号
        'TransactTime',     #SH-STOCK.OrderTime; SH-BOND.TickTime

        'Price',
        'OrderQty',
        'Side',       # 买/卖      #SH-BOND.TickBSFlag
        'OrdType',    # 深圳: 市价/限价/本方最优; 上海: 新增/删除

        'OrderNo',          #SH-STOCK; SH-BOND
        'BizIndex',         #SH-STOCK

    ]
    
    def __init__(self, SecurityIDSource=axsbe_base.SecurityIDSource_NULL, MsgType=axsbe_base.MsgType_order_stock):
        super(axsbe_order, self).__init__(MsgType, SecurityIDSource)
        self.Price = 0
        self.OrderQty = 0
        self.Side = 0
        self.OrdType = 0
        if self.MsgType==axsbe_base.MsgType_order_sse_bond_add:
            self.OrdType = ord('A')
        elif self.MsgType==axsbe_base.MsgType_order_sse_bond_del:
            self.OrdType = ord('D')

        self.OrderNo = 0
        self.BizIndex = 0

    def load_dict(self, dict:dict):
        '''从字典加载字段'''
        #公共头
        self.SecurityIDSource = dict['SecurityIDSource']
        self.MsgType = dict['MsgType']
        self.SecurityID = dict['SecurityID']
        self.ChannelNo = dict['ChannelNo']
        self.ApplSeqNum = dict['ApplSeqNum']
        
        # 深圳处理
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            self.Price = dict['Price']
            self.OrderQty = dict['OrderQty']
            self.Side = dict['Side']
            self.TransactTime = dict['TransactTime']
            self.OrdType = dict['OrdType']
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            self.OrderNo = dict['OrderNo']

            # 上海股票
            if self.MsgType==axsbe_base.MsgType_order_stock:
                self.Price = dict['Price']
                self.OrderQty = dict['OrderQty']
                self.OrdType = dict['OrdType']
                self.Side = dict['Side']
                self.TransactTime = dict['TransactTime']
                self.BizIndex = dict['BizIndex']

            # 上海债券, 
            # 上海债券 的 撤单是在 委托回报中的 (而上海股票撤单是在成交中的)
            elif self.MsgType==axsbe_base.MsgType_order_sse_bond_add or self.MsgType==axsbe_base.MsgType_order_sse_bond_del:
                self.Side = dict['TradingPhase']
                self.Qty = dict['Qty']
                self.TransactTime = dict['TickTime']
                if self.MsgType==axsbe_base.MsgType_order_sse_bond_add:
                    self.Price = dict['Price']
                    self.OrdType = ord('A')
                else:
                    self.OrdType = ord('D')
            else:
                raise Exception(f'Not support SSE order Type={self.MsgType}')
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')


    @property
    def Side_str(self):
        '''打印委托方向'''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            if self.Side==ord('1'):
                return '买入'
            elif self.Side==ord('2'):
                return '卖出'
            elif self.Side==ord('G'):   #TODO:暂无历史数据
                return '借入'
            elif self.Side==ord('F'):   #TODO:暂无历史数据
                return '出借'
            raise RuntimeError(f"非法委托方向:{self.Side}")
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.Side==ord('B'):
                return '买入'
            elif self.Side==ord('S'):
                return '卖出'
            raise RuntimeError(f"非法委托方向:{self.Side}")
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    @property
    def Type_str(self):
        '''打印委托类型'''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            if self.OrdType==ord('1'):
                return '市价'
            elif self.OrdType==ord('2'):
                return '限价'
            elif self.OrdType==ord('U'):
                return '本方最优'
            raise RuntimeError(f"非法委托类型:{self.OrdType}")
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.OrdType==ord('A'):
                return '新增'
            elif self.OrdType==ord('D'):
                return '删除'
            raise RuntimeError(f"非法委托类型:{self.OrdType}")
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    def setSide(self, s):
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            if s == "买入":
                self.Side = ord('1')
            elif s == "卖出":
                self.Side = ord('2')
            elif s == "借入":
                self.Side = ord('G')
            elif s == "出借":
                self.Side = ord('F')
            else:
                raise RuntimeError(f"非法委托方向:{s}")
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if s == "买入":
                self.Side = ord('B')
            elif s == "卖出":
                self.Side = ord('S')
            else:
                raise RuntimeError(f"非法委托方向:{s}")
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    def setType(self, t):
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            if t == "市价":
                self.OrdType = ord('1')
            elif t == "限价":
                self.OrdType = ord('2')
            elif t == "本方最优":
                self.OrdType = ord('U')
            else:
                raise RuntimeError(f"非法委托类型:{t}")
        elif self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if t == "新增":
                self.OrdType = ord('A')
            elif t == "删除":
                self.OrdType = ord('D')
            else:
                raise RuntimeError(f"非法委托类型:{t}")
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')


    def __str__(self):
        '''打印log，只有合法的SecurityIDSource才能被打印'''
        if self.SecurityIDSource == axsbe_base.SecurityIDSource_SZSE:
            return f'{"%06d"%self.SecurityID} T={self.Type_str + self.Side_str}, Px={self.Price}, Qty={self.OrderQty}, Seq={self.ApplSeqNum}, @{self.TransactTime}'
        elif  self.SecurityIDSource == axsbe_base.SecurityIDSource_SSE:
            if self.MsgType==axsbe_base.MsgType_order_stock:
                return f'{"%06d"%self.SecurityID} T={self.Type_str + self.Side_str}, Px={self.Price}, Qty={self.OrderQty}, Seq={self.ApplSeqNum}, OrderNo={self.OrderNo}, BizIndex={self.BizIndex}, @{self.TransactTime}'
            else:
                return f'{"%06d"%self.SecurityID} T={self.Type_str + self.Side_str}, Px={self.Price}, Qty={self.OrderQty}, Seq={self.ApplSeqNum}, OrderNo={self.OrderNo}, @{self.TransactTime}'
        else:
            raise Exception(f'Not support SecurityIDSource={self.SecurityIDSource}')

    @property
    def bytes_stream(self):
        return;
    def unpack_stream(self, bytes_i:bytes):
        return
    @property
    def ccode(self):
        return

    def save(self):
        return
    def load(self, data):
        return