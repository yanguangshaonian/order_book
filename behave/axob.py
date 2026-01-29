    # -*- coding: utf-8 -*-

'''
简单的行为模型，目标：
  * 只针对单只股票
  * 支持撮合和非撮合
  * 支持深圳、上海
  * 支持有涨跌停价、无涨跌停价
  * 支持创业板价格笼子
  * 支持股票和etf

  * 尽量倾向便于FPGA硬件实现，梳理流程，不考虑面向C/C++实现
  * 将遍历全市场以验证正确性，为提升验证效率，可能用C重写一遍
  * 主要解决几个课题：
    * 撮合是否必须保存每个价格档位的链表？
    * 出快照的时机，是否必须等10ms超时？
    * 位宽检查
    * 访问次数
    * save/load
'''
from enum import Enum
from tool.msg_util import axsbe_base, axsbe_exe, axsbe_order, axsbe_snap_stock, price_level, CYB_cage_upper, CYB_cage_lower, bitSizeOf, MARKET_SUBTYPE, market_subtype
import tool.msg_util as msg_util
from tool.axsbe_base import SecurityIDSource_SSE, SecurityIDSource_SZSE, INSTRUMENT_TYPE, MsgType_exe_sse_bond
from copy import deepcopy

import logging
axob_logger = logging.getLogger(__name__)

#### 静态工作开关 ####
EXPORT_LEVEL_ACCESS = False # 是否导出对价格档位的读写请求

#### 内部计算精度 ####
APPSEQ_BIT_SIZE = 32    # 序列号，34b，约40亿，因为不同channel的序列号各自独立，所以单channel整形就够
PRICE_BIT_SIZE  = 25    # 价格，20b，33554431，股票:335544.31;基金:33554.431。（创业板上市首日有委托价格为￥188888.00的，若忽略这种特殊情况，则20b(10485.75)足够了）
QTY_BIT_SIZE    = 30    # 数量，30b，(1,073,741,823)，深圳2位小数，上海3位小数
LEVEL_QTY_BIT_SIZE    = QTY_BIT_SIZE+8    # 价格档位上的数量位宽 20220729:001258 qty=137439040000(38b)
TIMESTAMP_BIT_SIZE = 28 # 时戳精度 时-分-秒-10ms 最大15000000=24b; 上交所精度1ms，最大28b

PRICE_INTER_STOCK_PRECISION = 100  # 股票价格精度：2位小数，(深圳原始数据4位，上海3位)
PRICE_INTER_FUND_PRECISION  = 1000 # 基金价格精度：3位小数，(深圳原始数据4位，上海3位)
PRICE_INTER_KZZ_PRECISION  = 1000 # 可转债格精度：3位小数，(深圳原始数据4位，上海3位)

QTY_INTER_SZSE_PRECISION   = 100   # 数量精度：深圳2位小数
QTY_INTER_SSE_PRECISION    = 1000  # 数量精度：上海3位小数

SZSE_TICK_CUT = 1000000000 # 深交所时戳，日期以下精度
SZSE_TICK_MS_TAIL = 10 # 深交所时戳，尾部毫秒精度，以10ms为单位

PRICE_MAXIMUM = (1<<PRICE_BIT_SIZE)-1

CYB_ORDER_ENVALUE_MAX_RATE = 9

class SIDE(Enum): # 2bit
    BID = 0
    ASK = 1

    UNKNOWN = -1    # 仅用于测试

    def __str__(self):
        if self.value==0:
            return 'BID'
        elif self.value==1:
            return 'ASK'
        else:
            return 'UNKNOWN'


class TYPE(Enum): # 2bit
    LIMIT  = 0   #限价
    MARKET = 1   #市价
    SIDE   = 2   #本方最优

    UNKNOWN = -1    # 仅用于测试

# 用于将原始精度转换到ob精度
SZSE_STOCK_PRICE_RD = msg_util.PRICE_SZSE_INCR_PRECISION // PRICE_INTER_STOCK_PRECISION
SZSE_FUND_PRICE_RD = msg_util.PRICE_SZSE_INCR_PRECISION // PRICE_INTER_FUND_PRECISION
SZSE_KZZ_PRICE_RD = msg_util.PRICE_SZSE_INCR_PRECISION // PRICE_INTER_KZZ_PRECISION
SSE_STOCK_PRICE_RD = msg_util.PRICE_SSE_PRECISION // PRICE_INTER_STOCK_PRECISION
# SSE_FUND_PRICE_RD = msg_util.PRICE_SSE_PRECISION // PRICE_INTER_FUND_PRECISION TODO:确认精度 [low priority]

class ob_order():
    '''专注于内部使用的字段格式与位宽'''
    __slots__ = [
        'applSeqNum',
        'price',
        'qty',  # 委托数量
        'side',  # 买卖方向
        'type',  # 限价 市价 本方最优

        # for test olny
        'traded',  # 这个委托 是否成交过
        'TransactTime',
    ]

    def __init__(self, order:axsbe_order, instrument_type:INSTRUMENT_TYPE):
        # self.securityID = order.SecurityID
        self.applSeqNum = order.ApplSeqNum

        if order.Side_str=='买入':
            self.side = SIDE.BID
        elif order.Side_str=='卖出':
            self.side = SIDE.ASK
        else:
            '''TODO-SSE'''
            self.side = SIDE.UNKNOWN

        if order.Type_str=='限价':          # SZ
            self.type = TYPE.LIMIT
        elif order.Type_str=='市价':        # SZ
            self.type = TYPE.MARKET
        elif order.Type_str=='本方最优':    # SZ
            self.type = TYPE.SIDE
        elif order.Type_str=='新增':        # SH
            self.type = TYPE.LIMIT
        else:
            self.type = TYPE.UNKNOWN

        # 溢出检查
        if order.Price==msg_util.ORDER_PRICE_OVERFLOW: #原始价格越界 (不用管是否是LIMIT)
            self.price = PRICE_MAXIMUM  #本地也按越界处理，本地越界最终只影响到卖出加权价的计算
            axob_logger.warn(f'{order.SecurityID:06d} order ApplSeqNum={order.ApplSeqNum} Price over the maximum!')
            assert not(self.side==SIDE.BID and self.type==TYPE.LIMIT), f'{order.SecurityID:06d} BID order price overflow' #限价买单不应溢出
        else:
            # 深圳
            if order.SecurityIDSource==SecurityIDSource_SZSE:
                if instrument_type==INSTRUMENT_TYPE.STOCK:
                    self.price = order.Price // SZSE_STOCK_PRICE_RD # 深圳 N13(4)，实际股票精度为分
                elif instrument_type==INSTRUMENT_TYPE.FUND:
                    self.price = order.Price // SZSE_FUND_PRICE_RD # 深圳 N13(4)，实际基金精度为厘
                elif instrument_type==INSTRUMENT_TYPE.KZZ:
                    self.price = order.Price // SZSE_KZZ_PRICE_RD # 深圳 N13(4)，实际基金精度为厘
                else:
                    axob_logger.error(f'order SZSE ApplSeqNum={order.ApplSeqNum} instrument_type={instrument_type} not support!')
            
            # 上海
            elif order.SecurityIDSource==SecurityIDSource_SSE:
                if instrument_type==INSTRUMENT_TYPE.STOCK:
                    self.price = order.Price // SSE_STOCK_PRICE_RD # 上海 原始数据3位小数
                elif instrument_type==INSTRUMENT_TYPE.BOND:
                    self.price = order.Price                       # 上海 原始数据3位小数，债券需要3位小数
                else:
                    axob_logger.error(f'order SSE ApplSeqNum={order.ApplSeqNum} instrument_type={instrument_type} not support!')
            else:
                self.price = 0
        
        # 新订单 没有成交过
        self.traded = False #仅用于测试：市价单，当有成交后，市价单的价格将确定
        self.TransactTime = order.TransactTime #仅用于测试：市价单，当有后续消息来而导致插入订单簿时，生成的订单簿用此时戳

        # 挂单数量
        self.qty = order.OrderQty    # 深圳2位小数;上海3位小数

        ## 位宽及精度舍入可行性检查
        if self.applSeqNum >= (1<<APPSEQ_BIT_SIZE) and self.applSeqNum!=0xffffffffffffffff:
            axob_logger.error(f'{order.SecurityID:06d} order ApplSeqNum={order.ApplSeqNum} ovf!')

        if self.price >= (1<<PRICE_BIT_SIZE):
            self.price = (1<<PRICE_BIT_SIZE)-1
            axob_logger.error(f'{order.SecurityID:06d} order ApplSeqNum={order.ApplSeqNum} Price={order.Price} ovf!')  # 无涨跌停价时可能，即使限价单也可能溢出，且会被前端处理成0x7fff_ffff

        if self.qty >= (1<<QTY_BIT_SIZE):
            axob_logger.error(f'{order.SecurityID:06d} order ApplSeqNum={order.ApplSeqNum} Volumn={order.OrderQty} ovf!')

        # 如果限价单 不是溢出值, 下面做检查
        if self.type==TYPE.LIMIT and order.Price!=msg_util.ORDER_PRICE_OVERFLOW:   #检查限价单价格是否溢出；市价单价格是无效值，不可参与检查

            if order.SecurityIDSource==SecurityIDSource_SZSE:
                if instrument_type==INSTRUMENT_TYPE.STOCK and order.Price % SZSE_STOCK_PRICE_RD:
                    axob_logger.error(f'{order.SecurityID:06d} order SZSE STOCK ApplSeqNum={order.ApplSeqNum} Price={order.Price} precision dnf!')  #当被前端处理成0x7fff_ffff时 会有余数
                elif instrument_type==INSTRUMENT_TYPE.FUND and order.Price % SZSE_FUND_PRICE_RD:
                    axob_logger.error(f'{order.SecurityID:06d} order SZSE FUND ApplSeqNum={order.ApplSeqNum} Price={order.Price} precision dnf!')  #当被前端处理成0x7fff_ffff时 会有余数
                elif instrument_type==INSTRUMENT_TYPE.KZZ and order.Price % SZSE_KZZ_PRICE_RD:
                    axob_logger.error(f'{order.SecurityID:06d} order SZSE KZZ ApplSeqNum={order.ApplSeqNum} Price={order.Price} precision dnf!')  #当被前端处理成0x7fff_ffff时 会有余数
            elif order.SecurityIDSource==SecurityIDSource_SSE:
                if instrument_type==INSTRUMENT_TYPE.STOCK and order.Price % SSE_STOCK_PRICE_RD:
                    axob_logger.error(f'{order.SecurityID:06d} order SSE STOCK ApplSeqNum={order.ApplSeqNum} Price={order.Price} precision dnf!')


class ob_exec():
    '''专注于内部使用的字段格式与位宽'''
    __slots__ = [
        'LastPx',
        'LastQty',
        'BidApplSeqNum',
        'OfferApplSeqNum',
        'TradingPhaseMarket',

        # for test olny
        'TransactTime',
    ]

    def __init__(self, exec:axsbe_exe, instrument_type:INSTRUMENT_TYPE):
        self.BidApplSeqNum = exec.BidApplSeqNum
        self.OfferApplSeqNum = exec.OfferApplSeqNum
        self.TradingPhaseMarket = exec.TradingPhaseMarket

        if exec.SecurityIDSource==SecurityIDSource_SZSE:
            if instrument_type==INSTRUMENT_TYPE.STOCK:
                self.LastPx = exec.LastPx // SZSE_STOCK_PRICE_RD # 深圳 N13(4)，实际股票精度为分
            elif instrument_type==INSTRUMENT_TYPE.FUND:
                self.LastPx = exec.LastPx // SZSE_FUND_PRICE_RD # 深圳 N13(4)，实际基金精度为厘
            elif instrument_type==INSTRUMENT_TYPE.KZZ:
                self.LastPx = exec.LastPx // SZSE_KZZ_PRICE_RD # 深圳 N13(4)，实际可转债精度为厘
            else:
                axob_logger.error(f'exec SZSE ApplSeqNum={exec.ApplSeqNum} instrument_type={instrument_type} not support!')
        elif exec.SecurityIDSource==SecurityIDSource_SSE:
            if instrument_type==INSTRUMENT_TYPE.STOCK:
                self.LastPx = exec.LastPx // SSE_STOCK_PRICE_RD # 上海 原始数据3位小数
            else:
                axob_logger.error(f'order SSE ApplSeqNum={exec.ApplSeqNum} instrument_type={instrument_type} not support!')
        else:
            self.LastPx = 0

        self.LastQty = exec.LastQty    # 深圳2位小数;上海3位小数

        self.TransactTime = exec.TransactTime

        ## 位宽及精度舍入可行性检查
        # 不去检查SeqNum位宽了，SeqNum总能在order list中找到，因此肯定已经检查过了。
        # price/qty同理
        # if self.LastPx >= (1<<PRICE_BIT_SIZE):
        #     axob_logger.error(f'{exec.SecurityID:06d} order ApplSeqNum={exec.ApplSeqNum} LastPx={exec.LastPx} ovf!')  # 无涨跌停价时可能，即使限价单也可能溢出，且会被前端处理成0x7fff_ffff

        # if self.LastQty >= (1<<QTY_BIT_SIZE):
        #     axob_logger.error(f'{exec.SecurityID:06d} order ApplSeqNum={exec.ApplSeqNum} LastQty={exec.LastQty} ovf!')



class ob_cancel():
    '''专注于内部使用的字段格式与位宽'''
    __slots__ = [
        'applSeqNum',
        'qty',
        'price',
        'side',

        # for test olny
        'TransactTime',
    ]
    def __init__(self, ApplSeqNum, Qty, Price, Side, TransactTime, SecurityIDSource, instrument_type, SecurityID):
        self.applSeqNum = ApplSeqNum    #
        self.qty = Qty
        if SecurityIDSource==SecurityIDSource_SZSE:
            self.price = 0  #深圳撤单不带价格
        elif SecurityIDSource==SecurityIDSource_SSE:
            if instrument_type==INSTRUMENT_TYPE.STOCK:
                self.price = Price // SSE_STOCK_PRICE_RD # 上海 原始数据3位小数
            else:
                axob_logger.error(f'{SecurityID:06d} cancel SSE ApplSeqNum={ApplSeqNum} instrument_type={instrument_type} not support!')
        else:
            axob_logger.error(f'{SecurityID:06d} cancel ApplSeqNum={ApplSeqNum} SecurityIDSource={SecurityIDSource} unknown!')
        self.side = Side

        self.TransactTime = TransactTime

        if self.applSeqNum >= (1<<APPSEQ_BIT_SIZE):
            axob_logger.error(f'{SecurityID:06d} cancel ApplSeqNum={ApplSeqNum} ovf!')

        if self.price >= (1<<PRICE_BIT_SIZE):
            axob_logger.error(f'{SecurityID:06d} cancel ApplSeqNum={ApplSeqNum} Price={Price} ovf!')

        if self.qty >= (1<<QTY_BIT_SIZE):
            axob_logger.error(f'{SecurityID:06d} cancel ApplSeqNum={ApplSeqNum} Volumn={Qty} ovf!')



class level_node():
    __slots__ = [
        'price',
        'qty',

        # for test olny
        # 'ts',
    ]
    def __init__(self, price, qty, ts):
        self.price = price
        self.qty = qty
    def __str__(self) -> str:
        return f'{self.price}\t{self.qty}'

class AX_SIGNAL(Enum):  # 发送给AXOB的信号
    OPENCALL_BGN  = 0  # 开盘集合竞价开始
    OPENCALL_END  = 1  # 开盘集合竞价结束
    AMTRADING_BGN = 2  # 上午连续竞价开始
    AMTRADING_END = 3  # 上午连续竞价结束
    PMTRADING_BGN = 4  # 下午连续竞价开始
    PMTRADING_END = 5  # 下午连续竞价结束
    ALL_END = 6        # 闭市

CHANNELNO_INIT = -1

class AXOB():
    __slots__ = [
        'SecurityID',
        'SecurityIDSource',
        'instrument_type',

        'order_map',    # map of ob_order, 保存了 所有的 委托号对应的订单详情, 当收到撤单或成交消息时 有时只有订单号需要根据订单号找到价格和数量
        'illegal_order_map',    # map of illegal_order  保存因超出涨跌幅限制或其他原因被判为非法但尚未处理完毕的订单（主要用于创业板无涨跌幅限制期间的特殊逻辑）  seqnum 是key, order 对象是 value
        'bid_level_tree', # map of level_node  买方/卖方价格树。以价格为 Key，保存该价位上的总挂单量（Qty）
        'ask_level_tree', # map of level_node  同上

        'NumTrades',  # 成交笔数

        # 买一价、买一量 卖一价、卖一量 撮合逻辑的核心判断依据。每当有新订单进来，首先与这些值比较以判断是否能成交（交叉）。如果发生从该档位的撤单/成交，需要触发“寻找下一档最优价”的逻辑
        'bid_max_level_price',
        'bid_max_level_qty',
        'ask_min_level_price',
        'ask_min_level_qty',

        # 统计类字段 最新、最高、最低、开盘价
        'LastPx',
        'HighPx',
        'LowPx',
        'OpenPx',

        # 收盘和 准备处理的标志位
        'closePx_ready',        # 默认为 false 否已经最终确定并准备好用于生成闭市快照
                                # . 核心背景：收盘价是如何产生的？
                                #     根据交易所规则（尤其是深交所），收盘价的产生有两种情况：
                                #     收盘集合竞价产生成交：集合竞价撮合成功，产生的成交价即为收盘价。
                                #     收盘集合竞价未产生成交：如果集合竞价期间买卖盘无法撮合（如买一价 < 卖一价），则收盘价通常回退取“当日该证券最后一笔交易前一分钟所有交易的成交量加权平均价”（深交所规则），或者直接取最后一笔成交价（视具体规则而定）。
                                #     问题在于：如果是情况 2，本地订单簿模型（AXOB）可能无法仅凭手头的逐笔数据精确计算出交易所最终认定的那个“加权平均价”或“官方收盘价”。因此，必须等待交易所发来的**闭市快照（Snapshot）**来“官宣”这个价格。
        
        'constantValue_ready',  # 默认为 false 收到 第一个状态Starting 切换消息之后 改为 true, 所有的 数据处理都要检查这个标志位

        'ChannelNo',    # 通道号
        'PrevClosePx',  # 昨收价 用于计算涨跌停、价格笼子基准
        'DnLimitPx',    # 跌停价 快照原始值
        'UpLimitPx',    # 涨停价 快照原始值
        'DnLimitPrice', # 跌停价 内部精度处理后的
        'UpLimitPrice', # 涨停价 内部精度处理后的
        'YYMMDD',       # 日期

        'current_inc_tick',  # 整数。表示当前处理到的最新一笔逐笔消息的时间戳（

        'BidWeightSize',  # 买方总委托量
        'BidWeightValue', # 买方总委托金额
        'AskWeightSize',  # 卖方总委托量
        'AskWeightValue', # 卖方总委托金额
        'AskWeightSizeEx', # 排除统计（如创业板价格笼子外）的委托量
        'AskWeightValueEx', # 排除统计（如创业板价格笼子外）的委托额

        'TotalVolumeTrade',
        'TotalValueTrade',

        # 交易逻辑缓冲
        # 缓存“待定”订单
        # 当收到一个市价单或本方最优单时，由于不知道具体的成交价格（取决于当时的对手盘），不能立即插入订单簿。系统将其暂存在 holding_order 中
        # 紧接着收到对应的逐笔成交消息时，利用成交消息中的价格来确定 holding_order 的实际价格，处理完所有成交后，再将剩余部分（如果有）插入 order_map 和价格树
        # 如果收到了新的委托, 表示上一个委托已经结束了撮合, 需要处理 holding_order
        'holding_order',  # 保存了 ob_order 对象
        'holding_nb',     # 计数

        'TradingPhaseMarket',     # 市场交易阶段
        # 'VolatilityBreaking_end_tick',

        'AskWeightPx_uncertain',    # 卖方加权均价不确定标志, insertOrder的时候, order.price==PRICE_MAXIMUM 的订单这个字段会被设置为 true

        'market_subtype',  # 市场子类型（主板/创业板/科创板等）。
        'bid_cage_upper_ex_min_level_price', # 买方笼子上限之外的最低价格档（及其数量） 买一价的头号候补梯队的兵力, 紧贴这买一价
        'bid_cage_upper_ex_min_level_qty',   # 买方笼子上限之外的最低价格档（及其数量） 买一价的头号候补梯队的兵力
        'ask_cage_lower_ex_max_level_price', # 卖方笼子下限之外的最高价格档。   就是紧贴着卖一价的
        'ask_cage_lower_ex_max_level_qty',   # 卖方笼子下限之外的最高价格档。
        'bid_cage_ref_px', # 基准价格。笼子的参考锚点，通常随成交价或一档价格变动, 用来计算买方价格笼子的上限 (Upper Limit)
        'ask_cage_ref_px', # 基准价格。笼子的参考锚点，通常随成交价或一档价格变动
        # 基准价获取顺序为：对手方一档 -> 本方一档 -> 最近成交价.
        
        # 标记“由于盘口最优价格的变化，可能导致原本在笼子外的隐藏订单进入笼子”这一状态，以便正确处理随后的成交消息
        # 他为 true 意味着 买/卖 一价（Best Bid）发生了变化, 参考价可能随之改变, 笼子的范围也可能平移
        # 当一个新的限价单插入，且价格优于当前最优价（买单更高，或卖单更低）时，会改变对手方的参考价
        'bid_waiting_for_cage',  # 护哪些订单在“笼子内”（参与撮合），哪些在“笼子外”（暂时隐藏）
        'ask_waiting_for_cage',  # 当一笔成交发生导致 LastPx（最新价）变化，进而导致 ref_px（基准价）变化时，系统会检查这些字段，将原本在笼子外的订单“释放”进入核心订单簿（...level_tree），或将新订单关入笼子

        # profile
        'pf_order_map_maxSize',
        'pf_level_tree_maxSize',
        'pf_bid_level_tree_maxSize',
        'pf_ask_level_tree_maxSize',
        'pf_AskWeightSize_max',
        'pf_AskWeightValue_max',
        'pf_BidWeightSize_max',
        'pf_BidWeightValue_max',

        # for test olny
        'msg_nb',
        'rebuilt_snaps',    # list of snap
        'market_snaps',     # list of snap
        'last_snap',
        'last_inc_applSeqNum',  # 这个上一次处理的消息号

        'logger',
        'DBG',
        'INFO',
        'WARN',
        'ERR',
    ]
    def __init__(self, SecurityID:int, SecurityIDSource, instrument_type:INSTRUMENT_TYPE, load_data=None):
        '''
        TODO: holding_order的处理是否统一到一处？必须要实现！
        TODO: 增加时戳输入，用于结算各自缓存，如市价单
        '''
        if load_data:
            self.load(load_data)
        else:
            self.SecurityID = SecurityID
            self.SecurityIDSource = SecurityIDSource #"证券代码源101=上交所;102=深交所;103=香港交易所" 在hls中用宏或作为模板参数设置
            self.instrument_type = instrument_type

            ## 结构数据：
            self.order_map = {} #订单队列，以applSeqNum作为索引
            self.illegal_order_map = {} #
            self.bid_level_tree = {} #买方价格档，以价格作为索引
            self.ask_level_tree = {} #卖方价格档

            self.NumTrades = 0
            self.bid_max_level_price = 0
            self.bid_max_level_qty = 0
            self.ask_min_level_price = 0
            self.ask_min_level_qty = 0
            self.LastPx = 0
            self.HighPx = 0
            self.LowPx = 0
            self.OpenPx = 0

            self.closePx_ready = False

            self.constantValue_ready = False
            self.ChannelNo = CHANNELNO_INIT #来自于快照
            self.PrevClosePx = 0 #来自于快照 深圳要处理到内部精度，用于在还原快照时比较
            self.DnLimitPx = 0  # #来自于快照 无涨跌停价时为0x7fffffff
            self.UpLimitPx = 0  # #来自于快照 无涨跌停价时为100
            self.DnLimitPrice = 0  
            self.UpLimitPrice = 0  
            self.YYMMDD = 0     #来自于快照
            self.current_inc_tick = 0 #来自于逐笔 时-分-秒-10ms
            
            self.BidWeightSize = 0
            self.BidWeightValue = 0
            self.AskWeightSize = 0
            self.AskWeightValue = 0
            self.AskWeightSizeEx = 0
            self.AskWeightValueEx = 0

            self.TotalVolumeTrade = 0
            self.TotalValueTrade = 0

            self.holding_order = None
            self.holding_nb = 0

            self.TradingPhaseMarket = axsbe_base.TPM.Starting    # 市场交易阶段

            self.AskWeightPx_uncertain = False #卖出加权价格无法确定（由于卖出委托价格越界）

            self.market_subtype = market_subtype(SecurityIDSource, SecurityID)

            self.bid_cage_upper_ex_min_level_price = 0 #买方价格笼子上沿之外的最低价，超过买入基准价的102%
            self.bid_cage_upper_ex_min_level_qty = 0
            self.ask_cage_lower_ex_max_level_price = 0 #卖方价格笼子下沿之外的最高价，低于卖出基准价的98%
            self.ask_cage_lower_ex_max_level_qty = 0
            self.bid_cage_ref_px = 0 #买方价格笼子基准价格 卖方一档价格 -> 买方一档价格 -> 最近成交价 -> 前收盘价，小于等于基准价的102%的在笼子内，大于的在笼子外（被隐藏）
            self.ask_cage_ref_px = 0 #卖方价格笼子基准价格 买方一档价格 -> 卖方一档价格 -> 最近成交价 -> 前收盘价，大于等于基准价的98%的在笼子内，小于的在笼子外（被隐藏）
            self.bid_waiting_for_cage = False
            self.ask_waiting_for_cage = False

            ## 调试数据，仅用于测试算法是否正确：
            self.pf_order_map_maxSize = 0
            self.pf_level_tree_maxSize = 0
            self.pf_bid_level_tree_maxSize = 0
            self.pf_ask_level_tree_maxSize = 0
            self.pf_AskWeightSize_max = 0
            self.pf_AskWeightValue_max = 0
            self.pf_BidWeightSize_max = 0
            self.pf_BidWeightValue_max = 0


            self.msg_nb = 0
            self.rebuilt_snaps = {}
            self.market_snaps = {}
            self.last_snap = None
            self.last_inc_applSeqNum = 0

            ## 日志
            self.logger = logging.getLogger(f'{self.SecurityID:06d}')
            g_logger = logging.getLogger('main')
            self.logger.setLevel(g_logger.getEffectiveLevel())
            axob_logger.setLevel(g_logger.getEffectiveLevel())
            for h in g_logger.handlers:
                self.logger.addHandler(h)
                axob_logger.addHandler(h) #这里补上模块日志的handler，有点ugly TODO: better way [low prioryty]

            self.DBG = self.logger.debug
            self.INFO = self.logger.info
            self.WARN = self.logger.warning
            self.ERR = self.logger.error

    def onMsg(self, msg):
        '''处理总入口'''
        if isinstance(msg, (axsbe_order, axsbe_exe, axsbe_snap_stock)):
            if msg.SecurityID!=self.SecurityID:
                return

            # 深交所：始终逐笔序列号递增，这里做检查
            # 上交所：非合并流逐笔会乱序，不检查
            # 这里是3个条件 必须是深圳股票, 必须是逐笔数据, 如果当前数据小于等于 上次处理的数据 说明 乱续了
            if self.SecurityIDSource==SecurityIDSource_SZSE and isinstance(msg, (axsbe_order, axsbe_exe)) and msg.ApplSeqNum<=self.last_inc_applSeqNum:
                self.ERR(f"ApplSeqNum={msg.ApplSeqNum} <= last_inc_applSeqNum={self.last_inc_applSeqNum} repeated or outOfOrder!")
                # 打印完了日志, 直接返回
                return

            if isinstance(msg, (axsbe_order, axsbe_exe)):
                # 这里会有问题, 所以放到了下面实现: 
                #     1. 缓存单问题：在连续竞价结束瞬间，可能还有未处理的“缓存单”（holding_order，通常是等待成交的市价单或特定限价单）。
                #          必须先处理完这些缓存单（尝试插入或撤销），然后再切换到收盘集合竞价状态。
                #          新的信号处理逻辑显式检查了 if self.holding_nb == 0，确保了状态切换的安全性。
                # 所以下面的代码注释了
                # if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM and self.TradingPhaseMarket==axsbe_base.TPM.PMTrading and msg.TradingPhaseMarket==axsbe_base.TPM.CloseCall:
                #     # 创业板进入收盘集合竞价，敞开价格笼子，将外面的隐藏订单放进来
                #     self.openCage()
                #     self.genSnap()

                assert self.constantValue_ready, f'{self.SecurityID:06d} constant values not ready!'

                # 更新一下当前处理的数据最新时间
                self._useTimestamp(msg.TransactTime)
                
                # 如果不是处在波动性中断状态 (防止错误覆盖), 才进行状态切换
                # 这里主动更新状态, 如果没有主动, 那就被动等待 MU广播进入 elif isinstance(msg, AX_SIGNAL): 的分支
                # 这里如果 msg如果是波动性中断, 那这里是切换, 给self也赋值, 
                # 在 onTrade中进行恢复到连续竞价阶段, 波动性中断（临停）结束的标志是进行一次集合竞价撮合。当这次撮合完成（即买卖盘不再交叉）时，状态就会切换回连续竞价。
                if self.TradingPhaseMarket!=axsbe_base.TPM.VolatilityBreaking:
                    # 那就把外部传来的消息内的状态 更新到当前对象中
                    self.TradingPhaseMarket = msg.TradingPhaseMarket # 只用逐笔，在阶段切换期间，逐笔和快照的速率不同，可能快照切了逐笔没切，或反过来，
                                                                    # 由于我们重建完全基于逐笔，快照仅用来做检查，故阶段切换基于逐笔。
                                                                    # 几个例外情况：
                                                                    #   在开盘集合竞价结束时可能没有成交；在进入中午休市时，没有逐笔。
                                                                    # 此时由更高层触发SIGNAL。
                # else:
                #     if self.VolatilityBreaking_end_tick==0: #波动性中断期间，逐笔成交到来说明中断结束
                #         if isinstance(msg, axsbe_exe) and msg.ExecType_str=='成交':
                #             self.VolatilityBreaking_end_tick = self.current_inc_tick
                #     else:
                #         if not(isinstance(msg, axsbe_exe) and msg.ExecType_str=='成交'): #中断结束后，有非逐笔成交
                #             self.TradingPhaseMarket = msg.TradingPhaseMarket

                

            if isinstance(msg, axsbe_order):
                self.onOrder(msg)
            elif isinstance(msg, axsbe_exe):
                self.onExec(msg)
            elif isinstance(msg, axsbe_snap_stock):
                self.onSnap(msg)

            # 深交所：始终逐笔序列号递增，这里做记录
            # 上交所：非合并流逐笔会乱序，不记录
            if self.SecurityIDSource==SecurityIDSource_SZSE and isinstance(msg, (axsbe_order, axsbe_exe)):
                self.last_inc_applSeqNum = msg.ApplSeqNum
        
        elif isinstance(msg, AX_SIGNAL):
            if msg==AX_SIGNAL.OPENCALL_END:
                if self.bid_max_level_price<self.ask_min_level_price and self.TradingPhaseMarket==axsbe_base.TPM.OpenCall: #双方最优价无法成交，否则等成交
                    self.TradingPhaseMarket = axsbe_base.TPM.PreTradingBreaking #自行修改交易阶段，使生成的快照为交易快照
                    self.genSnap()
            elif msg==AX_SIGNAL.AMTRADING_BGN:
                if self.TradingPhaseMarket==axsbe_base.TPM.PreTradingBreaking:
                    self.TradingPhaseMarket = axsbe_base.TPM.AMTrading
                    self.AskWeightSize += self.AskWeightSizeEx
                    self.AskWeightValue += self.AskWeightValueEx
                    self.genSnap()
            elif msg==AX_SIGNAL.AMTRADING_END:
                if self.TradingPhaseMarket==axsbe_base.TPM.AMTrading:
                    if self.holding_nb and self.holding_order.type==TYPE.MARKET:
                        self.insertOrder(self.holding_order)
                        self.holding_nb = 0
                    if self.holding_nb==0: #不再有缓存单
                        self.TradingPhaseMarket = axsbe_base.TPM.Breaking
                        self.genSnap()
            elif msg==AX_SIGNAL.PMTRADING_END:
                if self.TradingPhaseMarket==axsbe_base.TPM.PMTrading:
                    if self.holding_nb and self.holding_order.type==TYPE.MARKET:
                        self.insertOrder(self.holding_order)
                        self.holding_nb = 0
                    if self.holding_nb==0: #不再有缓存单
                        self.genSnap() # 先生成最后一个快照

                        self.TradingPhaseMarket = axsbe_base.TPM.CloseCall #自行修改交易阶段，使生成的快照为集合竞价快照
                        self.openCage() #开笼子，再生成集合竞价
                        self.genSnap()
            elif msg==AX_SIGNAL.ALL_END:
                # 收盘集合竞价结束，收盘价：
                #  沪市收盘价为当日该证券最后一笔交易前一分钟所有交易的成交量加权平均价（含最后一笔交易）。当日无成交的，以前收盘价为当日收盘价。
                #  深市的收盘价通过集合竞价的方式产生。收盘集合竞价不能产生收盘价的，以当日该证券最后一笔交易前一分钟所有交易的成交量加权平均价(含最后一笔交易)为收盘价。当日无成交的，以前收盘价为当日收盘价。 
                if self.SecurityIDSource==SecurityIDSource_SZSE:
                    if self.bid_max_level_price<self.ask_min_level_price and self.TradingPhaseMarket==axsbe_base.TPM.CloseCall: #双方最优价无法成交，否则等成交
                        self.TradingPhaseMarket = axsbe_base.TPM.Ending #自行修改交易阶段，使生成的快照为交易快照
                        self.closePx_ready = False  #等快照的价格作为最后价格
                    else:
                        self.closePx_ready = True   #直接生成快照
                        self.genSnap()
                else:
                    if self.bid_max_level_price<self.ask_min_level_price and self.TradingPhaseMarket==axsbe_base.TPM.CloseCall: #双方最优价无法成交，否则等成交
                        self.TradingPhaseMarket = axsbe_base.TPM.Ending #自行修改交易阶段，使生成的快照为交易快照
                    self.closePx_ready = False  #等快照的价格作为最后价格
        else:
            pass


        #if self.TradingPhaseMarket>=axsbe_base.TPM.Ending:
        # if self.msg_nb>=885:
        #    self._print_levels()


        ## 调试数据，仅用于测试算法是否正确：
        self.msg_nb += 1
        self.profile()

        if len(self.ask_level_tree):
            if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM and self.ask_cage_lower_ex_max_level_qty:
                assert self.ask_min_level_price>self.ask_cage_lower_ex_max_level_price, f'{self.SecurityID:06d} cache ask-min-price/cage-max NG'
            else:
                assert self.ask_min_level_price==min(self.ask_level_tree.keys()), f'{self.SecurityID:06d} cache ask-min-price NG'
                assert self.ask_min_level_qty==min(self.ask_level_tree.items(), key=lambda x: x[0])[1].qty, f'{self.SecurityID:06d} cache ask-min-qty NG'
        if len(self.bid_level_tree):
            if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM and self.bid_cage_upper_ex_min_level_qty:
                assert self.bid_max_level_price<self.bid_cage_upper_ex_min_level_price, f'{self.SecurityID:06d} cache bid-max-price/cage-min NG'
            else:
                assert self.bid_max_level_price==max(self.bid_level_tree.keys()), f'{self.SecurityID:06d} cache bid-max-price NG'
                assert self.bid_max_level_qty==max(self.bid_level_tree.items(), key=lambda x: x[0])[1].qty, f'{self.SecurityID:06d} ache bid-max-qty NG'

        if (self.TradingPhaseMarket==axsbe_base.TPM.AMTrading or self.TradingPhaseMarket==axsbe_base.TPM.PMTrading) and self.bid_max_level_qty and self.ask_min_level_qty:
            assert self.bid_max_level_price<self.ask_min_level_price, f'{self.SecurityID:06d} bid.max({self.bid_max_level_price})/ask.min({self.ask_min_level_price}) NG @{self.current_inc_tick}'

        static_AskWeightSize = 0
        static_AskWeightValue = 0
        for _,l in self.ask_level_tree.items():
            assert l.qty<(1<<LEVEL_QTY_BIT_SIZE), f'{self.SecurityID:06d} ask level qty={l.qty} ovf @{self.current_inc_tick}'
            if (self.ask_cage_lower_ex_max_level_qty==0 or l.price>self.ask_cage_lower_ex_max_level_price):
                static_AskWeightSize += l.qty
                static_AskWeightValue += l.price * l.qty
        if self.TradingPhaseMarket>=axsbe_base.TPM.AMTrading:
            assert static_AskWeightSize==self.AskWeightSize, f'{self.SecurityID:06d} static AskWeightSize={static_AskWeightSize}, dynamic AskWeightSize={self.AskWeightSize}'
            assert static_AskWeightValue==self.AskWeightValue, f'{self.SecurityID:06d} static AskWeightSize={static_AskWeightValue}, dynamic AskWeightValue={self.AskWeightValue}'
        else:
            assert static_AskWeightSize==self.AskWeightSize + self.AskWeightSizeEx, f'{self.SecurityID:06d} static AskWeightSize={static_AskWeightSize}, dynamic AskWeightSize={self.AskWeightSize}+{self.AskWeightSizeEx}'
            assert static_AskWeightValue==self.AskWeightValue + self.AskWeightValueEx, f'{self.SecurityID:06d} static AskWeightSize={static_AskWeightValue}, dynamic AskWeightValue={self.AskWeightValue}+{self.AskWeightValueEx}'

        static_BidWeightSize = 0
        static_BidWeightValue = 0
        for _,l in self.bid_level_tree.items():
            assert l.qty<(1<<LEVEL_QTY_BIT_SIZE), f'{self.SecurityID:06d} bid level qty={l.qty} ovf @{self.current_inc_tick}'
            if self.bid_cage_upper_ex_min_level_qty==0 or l.price<self.bid_cage_upper_ex_min_level_price:
                static_BidWeightSize += l.qty
                static_BidWeightValue += l.price * l.qty
        assert static_BidWeightSize==self.BidWeightSize, f'{self.SecurityID:06d} static BidWeightSize={static_BidWeightSize}, dynamic BidWeightSize={self.BidWeightSize}'
        assert static_BidWeightValue==self.BidWeightValue, f'{self.SecurityID:06d} static BidWeightValue={self.BidWeightValue}, dynamic BidWeightValue={self.BidWeightValue}'

        for _,ls in self.market_snaps.items():
            assert len(ls)!=0, f'{self.SecurityID:06d} market snap not pop clean'


    def openCage(self):
        self.DBG('openCage')
        # self._print_levels()

        ## 创业板上市头5日连续竞价、复牌集合竞价、收盘集合竞价的有效竞价范围是最近成交价的上下10%
        if self.UpLimitPx==msg_util.ORDER_PRICE_OVERFLOW: #无涨跌停限制=创业板上市头5日 TODO: 更精确
            ex_p = []
            self._export_level_access(f'LEVEL_ACCESS ASK inorder_list_inc //remove invalid price')
            for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                if p>msg_util.CYB_match_upper(self.LastPx) or p<msg_util.CYB_match_lower(self.LastPx):
                    ex_p.append(p)
                    if not self.ask_cage_lower_ex_max_level_qty or p>self.ask_cage_lower_ex_max_level_price:    #属于被纳入动态统计的价格档
                        self.AskWeightSize -= l.qty
                        self.AskWeightValue -= p * l.qty
            for p in ex_p:
                self.ask_level_tree.pop(p)
                self._export_level_access(f'LEVEL_ACCESS ASK remove {p} //remove invalid price') #二叉树也不能边遍历边修改, TODO: 全部remove后再平衡？

            ex_p = []
            self._export_level_access(f'LEVEL_ACCESS BID inorder_list_dec //remove invalid price')
            for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
                if p>msg_util.CYB_match_upper(self.LastPx) or p<msg_util.CYB_match_lower(self.LastPx):
                    ex_p.append(p)
                    if not self.bid_cage_upper_ex_min_level_qty or p<self.bid_cage_upper_ex_min_level_price:    #属于被纳入动态统计的价格档
                        self.BidWeightSize -= l.qty
                        self.BidWeightValue -= p * l.qty
            for p in ex_p:
                self.bid_level_tree.pop(p)
                self._export_level_access(f'LEVEL_ACCESS BID remove {p} //remove invalid price') #二叉树也不能边遍历边修改, TODO: 全部remove后再平衡？


        if self.ask_cage_lower_ex_max_level_qty:
            self._export_level_access(f'LEVEL_ACCESS ASK inorder_list_inc while <={self.ask_cage_lower_ex_max_level_price} //openCage')
            for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                if p<=self.ask_cage_lower_ex_max_level_price:
                    self.AskWeightSize += l.qty
                    self.AskWeightValue += p * l.qty
                else:
                    break

            self.ask_cage_lower_ex_max_level_qty = 0
            self.ask_min_level_price = min(self.ask_level_tree.keys())
            self.ask_min_level_qty = self.ask_level_tree[self.ask_min_level_price].qty
            self._export_level_access(f'LEVEL_ACCESS ASK locate_min //openCage') #TODO: 直接在上面遍历时赋值

        if self.bid_cage_upper_ex_min_level_qty:
            self._export_level_access(f'LEVEL_ACCESS BID inorder_list_dec while >={self.bid_cage_upper_ex_min_level_price} //openCage')
            for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
                if p>=self.bid_cage_upper_ex_min_level_price:
                    self.BidWeightSize += l.qty
                    self.BidWeightValue += p * l.qty
                else:
                    break

            self.bid_cage_upper_ex_min_level_qty = 0
            self.bid_max_level_price = max(self.bid_level_tree.keys())
            self.bid_max_level_qty = self.bid_level_tree[self.bid_max_level_price].qty
            self._export_level_access(f'LEVEL_ACCESS BID locate_max //openCage') #TODO: 直接在上面遍历时赋值
        # self._print_levels()


    def onOrder(self, order:axsbe_order):
        '''
        逐笔订单入口，统一提取市价单、限价单的关键字段到内部订单格式
        跳转到处理限价单或处理撤单
        '''
        # self.WARN(f'msg#{self.msg_nb} onOrder:{order}')
        
        # 处理上一笔的市价单
        if self.holding_nb!=0: #把此前缓存的订单(市价/限价)插入LOB
            # 如果市价单 但是没有成交过, 那报个错
            if self.holding_order.type == TYPE.MARKET and not self.holding_order.traded:
                self.ERR(f'市价单 {self.holding_order} 未伴随成交')
            
            # 上一笔订单入簿
            self.insertOrder(self.holding_order)
            # 清理状态
            self.holding_nb = 0

            # 把缓存的订单时间 修正刀 当前状态里面
            self._useTimestamp(self.holding_order.TransactTime)
            self.genSnap()   #先出一个snap，时戳用市价单的
            self._useTimestamp(order.TransactTime)


        # 创建一个当前模块的 临时 订单对象
        if self.SecurityIDSource == SecurityIDSource_SZSE:
            _order = ob_order(order, self.instrument_type)
        elif self.SecurityIDSource == SecurityIDSource_SSE:
            # order or cancel
            if order.Type_str=='新增':
                _order = ob_order(order, self.instrument_type)
            
            # 上海的 撤单 是在 委托回报中的
            elif order.Type_str=='删除':
                if order.Side_str=='买入':
                    Side=SIDE.BID
                elif order.Side_str=='卖出':
                    Side=SIDE.ASK
                _cancel = ob_cancel(order.OrderNo, order.Qty, order.Price, Side, order.TransactTime, self.SecurityIDSource, self.instrument_type, self.SecurityID)
                self.onCancel(_cancel)
                return
        else:
            return


        if _order.type==TYPE.MARKET:
            # 市价单，都必须在开盘之后
            if self.bid_max_level_qty==0 and self.ask_min_level_qty==0:
                raise '未定义模式:市价单早于价格档' #TODO: cover [Mid priority]

            # 市价单，几种可能：
            #    * 对手方最优价格申报：有成交、最后挂在对方一档或者二档，需要等时戳切换、新委托、新撤单到来的时候插入快照
            #    * 即时成交剩余撤销申报：最后有撤单
            #    * 全额成交或撤销申报：最后有撤单

        elif _order.type==TYPE.SIDE:
            # 本方最优，两种可能：
            #    * 本方最优价格申报 转限价单
            #    * 最优五档即时成交剩余撤销申报：最后有撤单，如果本方没有价格，立即撤单
            if _order.side==SIDE.BID:
                if self.bid_max_level_price!=0 and self.bid_max_level_qty!=0:   #本方有量
                    _order.price = self.bid_max_level_price
                else:
                    _order.price = self.DnLimitPrice
                    self.WARN(f'order #{_order.applSeqNum} 本方最优买单 但无本方价格!')
            else:
                if self.ask_min_level_price!=0 and self.ask_min_level_qty!=0:   #本方有量
                    _order.price = self.ask_min_level_price
                else:
                    _order.price = self.UpLimitPrice
                    self.WARN(f'order #{_order.applSeqNum} 本方最优卖单 但无本方价格!')
        else:
            pass
        self.onLimitOrder(_order)


    def onLimitOrder(self, order:ob_order):
        # 集合竞价期间 (OpenCall / CloseCall)
        #    直接入列：集合竞价期间不立即撮合，所有订单（除了非法的）通常直接插入订单簿。
        #    创业板特殊处理：如果是创业板（GEM）且价格超过了特定的有效范围（如无涨跌停限制时的过高/过低价格），可能会被标记为废单或存入 illegal_order_map。
        #    生成快照：每次插入后，立即调用 genSnap() 生成一个新的快照，反映当前的集合竞价状态。
        # 连续竞价期间 (AMTrading / PMTrading)
        #    价格笼子检查 (创业板)：如果超出笼子 (outOfCage=True)：调用 insertOrder(..., outOfCage=True) 将其作为“隐藏单”插入（不参与撮合，不展示在盘口，但需记录在案），并生成快照
        # 波动性中断 (VolatilityBreaking)：
        #    如果有新订单到达，通常意味着临停结束，进入了临时的集合竞价状态。 直接调用 insertOrder 插入订单，并生成快照。


        # 如果是早盘集合竞价  或者 收盘集合竞价
        if self.TradingPhaseMarket==axsbe_base.TPM.OpenCall or self.TradingPhaseMarket==axsbe_base.TPM.CloseCall:
            # 这段代码是 深交所创业板（ChiNext） 在 无涨跌幅限制状态下（通常指新股上市前5个交易日），针对 集合竞价阶段（Open Call / Close Call） 的 有效申报价格范围检查与废单处理逻辑
            # 如果订单判定为无效（Illegal），它将被放入 illegal_order_map 而不进入订单簿参与撮合

            # 1. 初始化标记：默认不丢弃
            should_discard = False

            # 2. 创业板的特殊判断 必须是 创业板(GEM) 且 无涨跌幅限制(UpLimitPx为溢出值)
            if self.market_subtype == MARKET_SUBTYPE.SZSE_STK_GEM and self.UpLimitPx == msg_util.ORDER_PRICE_OVERFLOW:
                # 3. 第二层判断：根据交易阶段 (OpenCall vs CloseCall) 细分
                if self.TradingPhaseMarket == axsbe_base.TPM.OpenCall:
                    # [开盘集合竞价] 条件：买单 且 价格 > 昨日收盘价 * 最大倍率
                    # 即便是无涨跌幅限制的新股，交易所为了防止乌龙指或恶意操纵，通常规定申报价格不得高于发行价/昨收价的 900%（即超过9倍）
                    if order.side == SIDE.BID:
                        if order.price > self.PrevClosePx * CYB_ORDER_ENVALUE_MAX_RATE:
                            should_discard = True
                            
                elif self.TradingPhaseMarket == axsbe_base.TPM.CloseCall:
                    # [收盘集合竞价] 条件：价格超出 匹配价格 的上下界
                    # 为了清晰，先计算出上下界
                    # 根据深交所交易规则，在无涨跌幅限制证券的收盘集合竞价阶段，申报价格不得高于最近成交价的 110% 且不得低于最近成交价的 90%
                    upper_bound = msg_util.CYB_match_upper(self.LastPx)
                    lower_bound = msg_util.CYB_match_lower(self.LastPx)
                    
                    if order.price > upper_bound or order.price < lower_bound:
                        should_discard = True

            # 4. 执行逻辑
            # 如果是无效单
            if should_discard:
                # 创业板无涨跌停时(上市头5日)超出范围则丢弃
                self.illegal_order_map[order.applSeqNum] = order
            else:
                # 正常插入订单
                self.insertOrder(order)

                # 集合竞价阶段不存在“触发进笼”的动态博弈机制，所有未被废弃的合规订单应当立即参与撮合计算
                # 关闭笼子检查
                self.bid_waiting_for_cage = False
                self.ask_waiting_for_cage = False

            # 5. 最后生成快照 (无论是否丢弃都执行)
            self.genSnap()
        else:
            # 非集合竞价的分支, 连续竞价阶段

            # -------------------------------------------------------------------------
            # 1. 逻辑判断阶段：判断是否为创业板(GEM)价格笼子之外的废单/隐藏单
            # -------------------------------------------------------------------------
            is_gem_out_of_cage = False

            # 第一层：必须是创业板 (ChiNext/GEM)
            if self.market_subtype == MARKET_SUBTYPE.SZSE_STK_GEM:
                # 第二层：必须是限价单 (L2中市价单和本方最优通常已有确定性处理，笼子主要针对限价)
                if order.type == TYPE.LIMIT:
                    # 第三层：区分买卖方向进行价格判定
                    if order.side == SIDE.BID:
                        # 买单：价格 > 基准价的102% (上沿)
                        if order.price > CYB_cage_upper(self.bid_cage_ref_px):
                            is_gem_out_of_cage = True
                    
                    elif order.side == SIDE.ASK:
                        # 卖单：价格 < 基准价的98% (下沿)
                        if order.price < CYB_cage_lower(self.ask_cage_ref_px):
                            is_gem_out_of_cage = True

            # 分支 A: 创业板价格笼子外的订单
            if is_gem_out_of_cage:
                # 标记为 outOfCage=True，通常意味着不参与当前撮合，但可能暂存或丢弃(视交易所规则)
                self.insertOrder(order, outOfCage=True)
                self.genSnap() # 状态改变，生成快照

            # 分支 B: 波动性中断 (Volatility Breaking)  elif 意味着如果命中了笼子逻辑，就不走波动性中断逻辑
            elif self.TradingPhaseMarket == axsbe_base.TPM.VolatilityBreaking:
                # 波动性中断期间收到新订单，通常意味着临停结束，进入集合竞价阶段
                # 此时直接插入订单簿
                self.insertOrder(order)
                self.genSnap() # 状态改变，生成快照
            else:
                # 逻辑分支：判断是市价单、跨价限价单（需缓存等待成交），还是普通限价单（直接插入）
                
                # 1. 判断是否为市价单, 市价单 暂存起来
                if order.type == TYPE.MARKET:
                    self.holding_order = order
                    self.holding_nb += 1
                    self.DBG('hold MARKET-order')
                
                # 2. 判断是否为限价单
                else:
                    # 初始化标志位：判断该限价单是否跨越了买卖价差（即是否会立即成交）
                    is_crossing_spread = False

                    if order.side == SIDE.BID:
                        # 买单逻辑：如果卖方有挂单，且买价 >= 卖一价
                        if self.ask_min_level_qty > 0:
                            if order.price >= self.ask_min_level_price:
                                is_crossing_spread = True
                    
                    elif order.side == SIDE.ASK:
                        # 卖单逻辑：如果买方有挂单，且卖价 <= 买一价
                        if self.bid_max_level_qty > 0:
                            if order.price <= self.bid_max_level_price:
                                is_crossing_spread = True

                    # 3. 根据是否跨价决定后续操作
                    # 如果能满足成交, 那先缓存, 等待后续的成交回报(因为后续肯定会接着一个成交回报)
                    if is_crossing_spread:
                        # 情况 A: 跨价限价单，缓存住，等待后续成交消息确认
                        self.holding_order = order
                        self.holding_nb += 1
                        self.DBG('hold LIMIT-order')
                        
                        # 因为发生了即时成交，价格笼子等待状态重置
                        # 意味着这个限价单的价格已经“穿过”了对手方的一档价格（买价 $\ge$ 卖一价，或 卖价 $\le$ 买一价）。这意味着必然会立即发生交易
                        # 根据交易所规则，订单必须先与当前订单簿上可见的最优价格进行撮合
                        # 如果此时 waiting_for_cage 为 True，意味着“可能有隐藏订单等待进入”。如果在撮合前执行了“进笼子”逻辑，可能会把本不该参与当前撮合的隐藏订单拉进来，或者改变了基准价
                        # 此时即将发生撮合交易，必须冻结价格笼子的状态，防止隐藏订单在成交前错误地‘插队’或干扰当前撮合
                        # 意思就是别管价格笼子了，现在有单子要成交！先专心处理成交（Hold住等Exec消息），等成交完了，价格定了，我们再去 onExec 里重新计算笼子的问题
                        self.bid_waiting_for_cage = False
                        self.ask_waiting_for_cage = False

                        # 结束处理了, 不入簿
                    else:
                        # 非即时成交的普通限价单
                        # 情况 B: 普通限价单（未成交），直接插入订单簿
                        # 没有即时成交, 说明 新的价格 卡在了 买一和卖一中间, 更新买一价和卖一价
                        self.insertOrder(order)
                        
                        # 创业板特殊处理：尝试入笼
                        # 这里需要满足  连续竞价阶段, 并且ob_order的价格在创业板价格笼子内
                        # 卡在买/卖 一之间 说明有新的基准价格, 更新一下笼子
                        if self.market_subtype == MARKET_SUBTYPE.SZSE_STK_GEM:
                            self.enterCage()

                        self.genSnap()   # 生成快照

    def insertOrder(self, order:ob_order, outOfCage=False):
        '''
        订单入列，更新对应的价格档位数据
        outOfCage: 用于表达该订单是否超出了当前的“价格笼子”有效范围
        '''

        if outOfCage:
            self.DBG('outOfCage')

        # 记录这个订单
        self.order_map[order.applSeqNum] = order
        
        if order.side == SIDE.BID:
            # self._export_level_access(f'LEVEL_ACCESS BID locate {order.price} //insertOrder')
            # 如果订单簿已经存在这个价格
            if order.price in self.bid_level_tree:
                # 直接增加这个价格的对应的量
                self.bid_level_tree[order.price].qty += order.qty
                # self._export_level_access(f'LEVEL_ACCESS BID writeback {order.price} //insertOrder')

                # 判断价格身份 

                # 分支 A: 如果是买一价（笼内最高）, 同步更新缓存
                if order.price==self.bid_max_level_price:
                    self.bid_max_level_qty += order.qty
                
                # 分支 B: 如果价格是笼子外的最低
                if self.bid_cage_upper_ex_min_level_qty and order.price==self.bid_cage_upper_ex_min_level_price:
                    self.bid_cage_upper_ex_min_level_qty += order.qty
            else:
                # 如果是订单簿之外的 新价格 那就插入
                node = level_node(order.price, order.qty, order.applSeqNum)
                self.bid_level_tree[order.price] = node

                # self._export_level_access(f'LEVEL_ACCESS BID insert {order.price} //insertOrder')
                # 插入订单之后, 再检查是否要修正缓存, 需要判断, 只有没出笼的才需要判断更新
                # 如果没有超过了价格笼子, 说明是个有效订单, 可以参与撮合, 并对外展示
                if not outOfCage:

                    # 判断是否刷新了买一价
                    #     当前还没买单 或者 新价格 > 当前买一价
                    if self.bid_max_level_qty==0 or order.price>self.bid_max_level_price:  #买方出现更高价格, 比现有买一价高, 但是没有出笼子(上方判断)
                        self.bid_max_level_price = order.price  # 更新缓存
                        self.bid_max_level_qty = order.qty      # 更新缓存

                        # 更新卖方笼子 基准价
                        # 买一价变了，卖方的价格笼子基准价通常依赖于买一价，所以要同步更新
                        self.ask_cage_ref_px = order.price
                        self.DBG(f'Ask cage ref px={self.ask_cage_ref_px}')

                        # 如果没有卖单
                        # 如果没有对手盘（ask_min_level_qty为0）买方笼子的基准价也暂时由买一价决定。
                        if not self.ask_min_level_qty:  #没有对手价
                            self.bid_cage_ref_px = order.price
                            self.DBG(f'bid cage ref px={self.bid_cage_ref_px}')

                        # 既然“卖方笼子基准价”变了（可能变高了），那么卖方笼子上限可能提高。
                        # 必须通知系统去检查是否有卖方隐藏单可以放出来。
                        # 这个函数退出之后, 会返回到 OnLimitOrder 中, 判断 是否创业板特殊处理：尝试入笼, 然后执行 self.enterCage()
                        self.ask_waiting_for_cage: bool = self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM
                else:
                    # 这是个笼子外的单子
                    # 逻辑：我们要找“笼子上面，离笼子最近（价格最低）的那个买单”

                    self.DBG('Bid order out of cage.')
                    # 1. order.price > 基准价 (基本校验), 意思就是 新订单的价格高于基准价。 这是一个**“向上溢出”**的订单
                    # 2. (当前还没有候补记录) OR (新订单价格 < 当前记录的候补价格), 
                    #    假设基准价 10.00，笼子上限 10.20。 隐藏队列里有个 10.25 的单子（这是当前的“守门员”，因为它是最接近 10.20 的）。
                    #    现在来了一个 10.22 的单子。10.22 虽然也超出了 10.20（进不去笼子），但它比 10.25 更接近笼子。10.22 < 10.25，条件成立。新的守门员成立
                    # 这里判断冗余了, 直接比较order.price>self.bid_cage_ref_px 没问题 , 当前order的价格已经经过函数外部判断, 通过outOfCage传进来 说明已经大于基准价格了这里再防御性的判断一下
                    if order.price>self.bid_cage_ref_px and\
                        (self.bid_cage_upper_ex_min_level_qty==0 or order.price<self.bid_cage_upper_ex_min_level_price): #买方笼子之上出现更低价
                        # 更新候补信息
                        self.bid_cage_upper_ex_min_level_price = order.price
                        self.bid_cage_upper_ex_min_level_qty = order.qty
                        self.DBG(f'Refresh bid_cage_upper_ex_min_level_price={self.bid_cage_upper_ex_min_level_price} by new price')

            # 没进笼子的都统计一下, 买方委托总量以及 买方委托总金额
            if not outOfCage:
                self.BidWeightSize += order.qty
                self.BidWeightValue += order.price * order.qty

        elif order.side == SIDE.ASK:
            # self._export_level_access(f'LEVEL_ACCESS ASK locate {order.price} //insertOrder')

            # 如果已经存在订单
            if order.price in self.ask_level_tree:
                # 直接累加上去
                self.ask_level_tree[order.price].qty += order.qty
                # self._export_level_access(f'LEVEL_ACCESS ASK writeback {order.price} //insertOrder')
                # 刷新最优价格的成交量, 这是卖一价的成交量
                if order.price==self.ask_min_level_price:
                    self.ask_min_level_qty += order.qty

                # 如果有临近笼子外的单, 并且 当前订单价格 等于这个订单价格 那就累加数量
                if self.ask_cage_lower_ex_max_level_qty and order.price==self.ask_cage_lower_ex_max_level_price:
                    self.ask_cage_lower_ex_max_level_qty += order.qty
            else:
                node = level_node(order.price, order.qty, order.applSeqNum)
                self.ask_level_tree[order.price] = node
                # self._export_level_access(f'LEVEL_ACCESS ASK insert {order.price} //insertOrder')

                if order.price==PRICE_MAXIMUM:
                    self.AskWeightPx_uncertain = True #价格越界后，卖出均价将无法确定

                # 没有出笼子
                # 如果没有超过了价格笼子, 说明是个有效订单, 可以参与撮合, 并对外展示
                if not outOfCage:
                    # 如果卖一量为0, 或者 当前价格 比卖一价还要低, 说明卖方出现更低价格
                    if self.ask_min_level_qty==0 or order.price<self.ask_min_level_price: 
                        self.ask_min_level_price = order.price
                        self.ask_min_level_qty = order.qty

                        # 新的卖一价导致 买方笼子基准价可能变了
                        self.bid_cage_ref_px = order.price
                        # self.DBG(f'Bid cage ref px={self.bid_cage_ref_px}')

                        # 如果没有买方, 那么卖方笼子的价格 可以暂时用卖一价
                        if not self.bid_max_level_qty:  #没有对手价
                            self.ask_cage_ref_px = order.price
                            # self.DBG(f'Ask cage ref px={self.ask_cage_ref_px}')

                        # 最优价格变化 导致 笼子需要平移
                        self.bid_waiting_for_cage = self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM
                else:
                    # 出笼子了
                    # 如果委托价格比 基准价格低(废话), 并且, (目前笼子外还没有委托 或者 当前委托价格 大于 笼子外的最低价格)
                    # 就是价格 小于 ask_cage_ref_px 但是 大于 卖方笼子的外的最高价格, 那就更新卖方笼子外的最高价格 


                    # self.DBG('Ask order out of cage.')

                    if order.price<self.ask_cage_ref_px and\
                        (self.ask_cage_lower_ex_max_level_qty==0 or order.price>self.ask_cage_lower_ex_max_level_price): #卖方笼子之下出现更高价
                        self.ask_cage_lower_ex_max_level_price = order.price
                        self.ask_cage_lower_ex_max_level_qty = order.qty
                        # self.DBG(f'Refresh ask_cage_lower_ex_max_level_price={self.ask_cage_lower_ex_max_level_price} by new price')

            # 没有出笼子
            if not outOfCage:
                # 还属于集合竞价, 并且 价格超过昨收若干倍
                if self.TradingPhaseMarket==axsbe_base.TPM.OpenCall and order.price>self.PrevClosePx*CYB_ORDER_ENVALUE_MAX_RATE:  #从深交所数据上看，超过昨收(新股时为上市价)若干倍的委托不会参与统计
                    self.AskWeightSizeEx += order.qty
                    self.AskWeightValueEx += order.price * order.qty
                # if order.price<self.PrevClosePx*CYB_ORDER_ENVALUE_MAX_RATE and order.price!=(1<<PRICE_BIT_SIZE)-1:  
                else:
                    self.AskWeightSize += order.qty
                    self.AskWeightValue += order.price * order.qty

    def onExec(self, exec:axsbe_exe):
        '''
        逐笔成交入口
        跳转到处理成交或处理撤单
        '''
        self.DBG(f'msg#{self.msg_nb} onExec:{exec}')
        if exec.ExecType_str=='成交' or self.SecurityIDSource==SecurityIDSource_SSE:
            _exec = ob_exec(exec, self.instrument_type)
            self.onTrade(_exec)
        else:
            #only SecurityIDSource_SZSE
            if exec.BidApplSeqNum!=0:  # 撤销bid
                cancel_seq = exec.BidApplSeqNum
                Side = SIDE.BID
            else:   # 撤销ask
                cancel_seq = exec.OfferApplSeqNum
                Side = SIDE.ASK
            _cancel = ob_cancel(cancel_seq, exec.LastQty, exec.LastPx, Side, exec.TransactTime, self.SecurityIDSource, self.instrument_type, self.SecurityID)
            self.onCancel(_cancel)



    def onTrade(self, exec:ob_exec):
        '''处理成交消息'''
        #
        self.NumTrades += 1
        self.TotalVolumeTrade += exec.LastQty

        if self.SecurityIDSource==SecurityIDSource_SZSE:
            # 乘法输入：深圳(Qty精度2位、price精度2位or3位小数)；输出TotalValueTrade深圳(精度4位小数)
            if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                self.TotalValueTrade += int(exec.LastQty * exec.LastPx/(QTY_INTER_SZSE_PRECISION*PRICE_INTER_STOCK_PRECISION // msg_util.TOTALVALUETRADE_SZSE_PRECISION)) # 2x2->4
            elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                self.TotalValueTrade += int(exec.LastQty * exec.LastPx/(QTY_INTER_SZSE_PRECISION*PRICE_INTER_FUND_PRECISION // msg_util.TOTALVALUETRADE_SZSE_PRECISION)) # 2x3->4
            elif self.instrument_type==INSTRUMENT_TYPE.KZZ:
                self.TotalValueTrade += int(exec.LastQty * exec.LastPx/(QTY_INTER_SZSE_PRECISION*PRICE_INTER_KZZ_PRECISION // msg_util.TOTALVALUETRADE_SZSE_PRECISION)) # 2x3->4
            else:
                self.TotalValueTrade += None
        elif self.SecurityIDSource==SecurityIDSource_SSE:
            # 乘法输入：上海(Qty精度3位、price精度2位or3位小数)；输出TotalValueTrade上海(精度5位小数)
            if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                self.TotalValueTrade += int(exec.LastQty * exec.LastPx/(QTY_INTER_SSE_PRECISION*PRICE_INTER_STOCK_PRECISION // msg_util.TOTALVALUETRADE_SSE_PRECISION)) # 3x2 -> 5
            elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                self.TotalValueTrade += int(exec.LastQty * exec.LastPx/(QTY_INTER_SSE_PRECISION*PRICE_INTER_FUND_PRECISION // msg_util.TOTALVALUETRADE_SZSE_PRECISION)) # 3x3->5
            else:
                self.TotalValueTrade += None
        else:
            self.TotalValueTrade += None

        self.LastPx = exec.LastPx
        if self.OpenPx == 0:
            self.OpenPx = exec.LastPx
            self.HighPx = exec.LastPx
            self.LowPx = exec.LastPx
        else:
            if self.HighPx < exec.LastPx:
                self.HighPx = exec.LastPx
            if self.LowPx > exec.LastPx:
                self.LowPx = exec.LastPx

        #有可能市价单剩余部分进队列，后续成交是由价格笼子外的订单造成的
        if self.holding_nb and self.holding_order.type==TYPE.MARKET:
            if self.holding_order.applSeqNum!=exec.BidApplSeqNum and self.holding_order.applSeqNum!=exec.OfferApplSeqNum:
                self.WARN('MARKET order followed by unmatch exec, take as traded over!')
                assert self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM, f'{self.SecurityID:06d} not CYB'
                self.insertOrder(self.holding_order)
                self.holding_nb = 0

                self._useTimestamp(self.holding_order.TransactTime)
                self.genSnap()   #先出一个snap，时戳用市价单的
                self._useTimestamp(exec.TransactTime)
                

        if self.holding_nb!=0:
            # 紧跟缓存单的成交
            level_side = SIDE.ASK if exec.BidApplSeqNum==self.holding_order.applSeqNum else SIDE.BID #level_side:缓存单的对手盘
            self.DBG(f'level_side={level_side}')
            assert self.holding_order.qty>=exec.LastQty, f"{self.SecurityID:06d} holding order Qty unmatch"
            if self.holding_order.qty==exec.LastQty:
                self.holding_nb = 0
            else:
                self.holding_order.qty -= exec.LastQty

                if self.holding_order.type==TYPE.MARKET:   #修改市价单的价格
                    self.holding_order.price = exec.LastPx
                    self.holding_order.traded = True

            if level_side==SIDE.ASK:
                self.tradeLimit(SIDE.ASK, exec.LastQty, exec.OfferApplSeqNum)
            else:
                self.tradeLimit(SIDE.BID, exec.LastQty, exec.BidApplSeqNum)

            if self.holding_nb!=0 and self.holding_order.type==TYPE.LIMIT:  #检查限价单是否还有对手价
                if (self.holding_order.side==SIDE.BID and (self.holding_order.price<self.ask_min_level_price or self.ask_min_level_qty==0)) or \
                   (self.holding_order.side==SIDE.ASK and (self.holding_order.price>self.bid_max_level_price or self.bid_max_level_qty==0)):
                   # 对手盘已空，缓存单入列
                    self.insertOrder(self.holding_order)
                    self.holding_nb = 0

            if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM:
                self.enterCage()

            if self.holding_nb==0:
                self.genSnap()   #缓存单成交完
        elif self.bid_waiting_for_cage or self.ask_waiting_for_cage:
            self.DBG("Order entered cage & exec.")
            self.tradeLimit(SIDE.ASK, exec.LastQty, exec.OfferApplSeqNum)
            self.tradeLimit(SIDE.BID, exec.LastQty, exec.BidApplSeqNum)
            if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM:
                self.enterCage()
            self.genSnap()   #出一个snap
        else:
            assert self.holding_nb==0, f'{self.SecurityID:06d} unexpected exec while holding_nb!=0'
            #20221010 300654  碰到深交所订单乱序：先发送2档以上的逐笔成交，再发送1档的撤单（卖方1档撤单导致买方订单进入价格笼子，吃掉卖方2档及以上）；目前直接应用成交可以正常继续重建:
            if not ((exec.TransactTime%SZSE_TICK_CUT==92500000)or(exec.TransactTime%SZSE_TICK_CUT==150000000) if self.SecurityIDSource==SecurityIDSource_SZSE else (exec.TransactTime==9250000)or(exec.TransactTime==15000000)) and\
               self.TradingPhaseMarket!=axsbe_base.TPM.VolatilityBreaking:
                self.WARN(f'unexpected exec @{exec.TransactTime}!')

            self.tradeLimit(SIDE.ASK, exec.LastQty, exec.OfferApplSeqNum)
            self.tradeLimit(SIDE.BID, exec.LastQty, exec.BidApplSeqNum)

            if self.ask_min_level_qty==0 or self.bid_max_level_qty==0 or self.ask_min_level_price>self.bid_max_level_price:
                self.DBG('openCall/closeCall trade over')
                if self.TradingPhaseMarket==axsbe_base.TPM.VolatilityBreaking:
                    self.TradingPhaseMarket = exec.TradingPhaseMarket
                self.genSnap()   #集合竞价所有成交完成

    def enterCage(self):
        '''判断订单是否可进入笼子，若进入笼子，判断是否可以成交'''
        # 在创业板中，只有价格在基准价一定范围内（通常是 正负2%）的订单才是有效的。超出范围的订单会被暂时“隐藏”在笼子外。当基准价发生变化，笼子范围移动，原本在笼子外的订单可能“进入笼子”成为有效订单
        # 这个函数是一个死循环（While True），因为买方订单进入笼子会改变买一价，从而改变卖方的基准价，可能诱发卖方订单进入笼子，卖方变化又反过来影响买方，形成连锁反应
        # 任何可能导致“基准价”（Reference Price）发生变化的事件，都必须触发
        # 卖方笼子基准价 约等于 买一价 (Bid 1), 买方笼子基准价 约等于 卖一价 (Ask 1)
        # 基准价获取顺序为：对手方一档 -> 本方一档 -> 最近成交价.

        # 进笼推演 -> 发现成交可能 -> 立即暂停 -> 等实锤（逐笔成交）
        # 一旦有新订单进入笼子, 那就触发连锁反应, 直到找到第一个能够立即成交的单子,然后退出循环等待交易所来驱动下一步的动作
        while True:
            # 检查是否存在笼子外的买单（数量不为0）, 并且隐藏订单的价格 小于 基准价格的102%, 说明这个上限外 最低的买方隐藏订单可以进笼子了
            # 一旦进笼子 势必会修改 最优价格
            if self.bid_cage_upper_ex_min_level_qty and self.bid_cage_upper_ex_min_level_price<=CYB_cage_upper(self.bid_cage_ref_px): #买方隐藏订单可以进入笼子
                # 检查刚进笼子的买单是否能立刻成交 
                # self.ask_min_level_qty: 卖方有挂单
                # self.bid_cage_upper_ex_min_level_price >= self.ask_min_level_price: 买价 >= 卖一价（满足成交条件）
                # self.TradingPhaseMarket != ...VolatilityBreaking: 当前不是波动性中断状态（中断时不能成交）
                if self.ask_min_level_qty and self.bid_cage_upper_ex_min_level_price>=self.ask_min_level_price and self.TradingPhaseMarket!=axsbe_base.TPM.VolatilityBreaking: #可与卖方最优成交
                    # 如果能成交，不在这里处理更新逻辑，而是直接跳出循环。
                    # 外部逻辑（如 onExec 或 genSnap）会处理成交导致的订单簿变化。

                    # 在连续竞价阶段，订单簿的 买一价必须永远小于卖一价。
                    # 等待“逐笔成交”驱动 (Event Driven)
            
                    self.DBG(f'ASK px may changed: waiting for BID level' 
                             f'({self.bid_cage_upper_ex_min_level_price} x {self.bid_cage_upper_ex_min_level_qty}) to enter cage & exec')
                    break
                else:   #无法成交，将隐藏订单加到买方队列
                    # 修改买一价
                    self.bid_max_level_price = self.bid_cage_upper_ex_min_level_price
                    self.bid_max_level_qty = self.bid_cage_upper_ex_min_level_qty
                    self.BidWeightSize += self.bid_cage_upper_ex_min_level_qty
                    self.BidWeightValue += self.bid_cage_upper_ex_min_level_price * self.bid_cage_upper_ex_min_level_qty
                    self.DBG('BID order enter cage and became max level')
                    
                    # 修改 卖方基准价格
                    self.ask_cage_ref_px = self.bid_max_level_price
                    # self.DBG(f'ASK cage ref px={self.ask_cage_ref_px}')

                    # 如果卖方还没有对手盘, 那 买方的基准价格 暂时也由当前的买一价决定
                    if not self.ask_min_level_qty:
                        self.bid_cage_ref_px = self.bid_max_level_price
                        # self.DBG(f'Bid cage ref px={self.bid_cage_ref_px}')

                    # 买方的价格被修改了, 现在需要判断 卖方的隐藏订单
                    self.ask_waiting_for_cage = self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM   #买方最优价被修改，则判断卖方隐藏订单

                    # 寻找下一个隐藏订单，继续循环，直到无隐藏订单、隐藏订单可成交
                    self.bid_cage_upper_ex_min_level_qty = 0
                    # self._export_level_access(f'LEVEL_ACCESS BID locate_higher {self.bid_cage_upper_ex_min_level_price} //enterCage:find next order out of cage')
                    for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                        if p>self.bid_cage_upper_ex_min_level_price:
                            self.bid_cage_upper_ex_min_level_price = p
                            self.bid_cage_upper_ex_min_level_qty = l.qty
                            self.DBG(f'Refresh bid_cage_upper_ex_min_level_price={self.bid_cage_upper_ex_min_level_price} by prev bid level enter cage')
                            break
            else:
                # 买方最优价没有被修改
                # 如果没有买单能进笼子，重置标记，表示不需要特意等待买方检查
                self.bid_waiting_for_cage = False


            # 逻辑背景：卖出价格不能过低。ask_cage_lower_ex_max_level 指的是“笼子下限之外”的订单中价格最高的那个。
            # 如果基准价下跌，笼子下限降低，这个“下限外最高”的订单可能是第一个进入笼子的。

            # 检查是否有卖方隐藏订单，且该订单价格满足新的笼子下限
            # self.ask_cage_lower_ex_max_level_qty: 检查是否存在笼子外的卖单
            # self.ask_cage_lower_ex_max_level_price >= CYB_cage_lower(...): 如果隐藏卖单价格 >= 新下限，说明它现在合法了（不再被视为恶意砸盘），可以“进笼”。

            if self.ask_cage_lower_ex_max_level_qty and self.ask_cage_lower_ex_max_level_price>=CYB_cage_lower(self.ask_cage_ref_px):
                # 检查刚进笼子的卖单是否能立刻成交
                # self.bid_max_level_qty: 买方有挂单
                # self.ask_cage_lower_ex_max_level_price <= self.bid_max_level_price: 卖价 <= 买一价（满足成交条件）
                # self.TradingPhaseMarket != ...: 非波动性中断
                if self.bid_max_level_qty and self.ask_cage_lower_ex_max_level_price<=self.bid_max_level_price and self.TradingPhaseMarket!=axsbe_base.TPM.VolatilityBreaking: #可与买方最优成交
                    # 同样，如果能成交，跳出循环，交由外部逻辑处理成交
                    self.DBG(f'BID px may changed: waiting for ASK level'
                             f'({self.ask_cage_lower_ex_max_level_price} x {self.ask_cage_lower_ex_max_level_qty}) to enter cage & exec')
                    break
                else:   #无法成交，将隐藏订单加到买方队列
                    # 更新卖一价（ask_min）
                    self.ask_min_level_price = self.ask_cage_lower_ex_max_level_price
                    self.ask_min_level_qty = self.ask_cage_lower_ex_max_level_qty
                    # 累加统计数据
                    self.AskWeightSize += self.ask_cage_lower_ex_max_level_qty
                    self.AskWeightValue += self.ask_cage_lower_ex_max_level_price * self.ask_cage_lower_ex_max_level_qty
                    self.DBG('ASK order enter cage and became min level')

                    # 关键连锁逻辑：卖一价变了，会影响买方的基准价（bid_cage_ref_px）
                    self.bid_cage_ref_px = self.ask_min_level_price
                    # self.DBG(f'BID cage ref px={self.bid_cage_ref_px}')

                    # 特殊情况：如果买方本来是空的，卖方基准价参考自身卖一
                    if not self.bid_max_level_qty:
                        self.ask_cage_ref_px = self.ask_min_level_price
                        # self.DBG(f'Ask cage ref px={self.ask_cage_ref_px}')

                    # 因为买方基准价(bid_cage_ref_px)变了，需要通知下一轮循环去检查买方
                    self.bid_waiting_for_cage = self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM   #卖方最优价被修改，则判断买方隐藏订单

                    # 寻找下一个“笼子外最高卖单”
                    # 当前的进来了，找剩下还在外面的卖单里价格最高的（最接近有效区间的）
                    self.ask_cage_lower_ex_max_level_qty = 0
                    # self._export_level_access(f'LEVEL_ACCESS ASK locate_lower {self.ask_cage_lower_ex_max_level_price} //enterCage:find next order out of cage')
                    for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
                        if p<self.ask_cage_lower_ex_max_level_price:
                            self.ask_cage_lower_ex_max_level_price = p
                            self.ask_cage_lower_ex_max_level_qty = l.qty
                            # self.DBG(f'Refresh ask_cage_lower_ex_max_level_price={self.ask_cage_lower_ex_max_level_price} by prev ask level enter cage')
                            break
            else:
                # 如果没有卖单能进笼子，重置标记
                self.ask_waiting_for_cage = False

            # 如果买方不需要等待检查笼子 (bid_waiting_for_cage is False)
            # 且 卖方也不需要等待检查笼子 (ask_waiting_for_cage is False)
            # 说明基准价已经稳定，没有新的订单因为基准价变化而进入笼子，连锁反应结束。
            if not self.bid_waiting_for_cage and not self.ask_waiting_for_cage:
                break


    def tradeLimit(self, side:SIDE, Qty, appSeqNum):
        if appSeqNum not in self.order_map:
            self.ERR(f'traded order #{appSeqNum} not found!')
        order = self.order_map[appSeqNum]
        # order.qty -= Qty
        self.levelDequeue(side, order.price, Qty, appSeqNum)

    def onCancel(self, cancel:ob_cancel):
        '''
        处理撤单，来自深交所逐笔成交或上交所逐笔成交
        撤销此前缓存的订单(市价/限价)，或插入LOB
        '''
        if self.holding_nb!=0:    #此处缓存的应该都是市价单
            self.holding_nb = 0

            if True:
                ## 仅测试：不论撤销的是不是缓存单，都将缓存单插入OB并生成快照用于比较
                ##  因为市场快照可能是缓存单插入后的快照
                self.insertOrder(self.holding_order)
                if cancel.TransactTime!=self.holding_order.TransactTime:    #这个if是为了规避 最优五档即时成交剩余撤销申报 在撤单时没有发生过成交但插入的价格不对的问题，切换到实际操作是不会有这个问题。
                    self._useTimestamp(self.holding_order.TransactTime)
                    self.genSnap()   #先出一个snap，时戳用缓存单(市价单)的
                    self._useTimestamp(cancel.TransactTime)

            else:
                ## 实际操作，如果撤销的是缓存单，则不需要插入OB：
                if self.holding_order.applSeqNum!=cancel.applSeqNum: #撤销的不是缓存单，把缓存单插入LOB
                    self.insertOrder(self.holding_order)
                    self._useTimestamp(self.holding_order.TransactTime)
                    self.genSnap()   #先出一个snap，时戳用缓存单(市价单)的
                    self._useTimestamp(cancel.TransactTime)
                if self.holding_order.applSeqNum==cancel.applSeqNum: #撤销缓存单，holding_nb清空即可
                    return  

        if cancel.applSeqNum in self.order_map:
            order = self.order_map.pop(cancel.applSeqNum)   # 注意order.qty是旧值。实际可以不用pop。

            self.levelDequeue(cancel.side, order.price, cancel.qty, cancel.applSeqNum)
            if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM:
                self.enterCage()

            self.genSnap()
        elif cancel.applSeqNum in self.illegal_order_map:
            self.illegal_order_map.pop(cancel.applSeqNum)
        else:
            self.ERR(f'cancel AppSeqNum={cancel.applSeqNum} not found!')
            raise 'cancel AppSeqNum not found!'

    def levelDequeue(self, side, price, qty, applSeqNum):
        '''买/卖方价格档出列（撤单或成交时）'''
        if side == SIDE.BID:
            self.bid_level_tree[price].qty -= qty
            self._export_level_access(f'LEVEL_ACCESS BID locate {price} //levelDequeue')
            # self.bid_level_tree[price].ts.remove(applSeqNum)
            if price==self.bid_max_level_price:
                self.bid_max_level_qty -= qty

            if self.bid_cage_upper_ex_min_level_qty==0 or price<self.bid_cage_upper_ex_min_level_price:
                self.BidWeightSize -= qty
                self.BidWeightValue -= price * qty
            elif self.bid_cage_upper_ex_min_level_qty and price==self.bid_cage_upper_ex_min_level_price:
                self.bid_cage_upper_ex_min_level_qty -= qty
                if self.bid_cage_upper_ex_min_level_qty==0: #买方价格笼子外最低价被cancel/trade光
                    # locate next high bid level
                    self._export_level_access(f'LEVEL_ACCESS BID locate_higher {self.bid_cage_upper_ex_min_level_price} //levelDequeue:find next level out of cage')
                    for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历，TODO:可以先判断是否存在
                        if p>self.bid_cage_upper_ex_min_level_price:
                            self.bid_cage_upper_ex_min_level_price = p
                            self.bid_cage_upper_ex_min_level_qty = l.qty
                            self.DBG(f'Refresh bid_cage_upper_ex_min_level_price={self.bid_cage_upper_ex_min_level_price} by canceled/traded all')
                            break

            if self.bid_level_tree[price].qty==0:
                if price==self.bid_max_level_price:  #买方最高价被cancel/trade光
                    self.bid_max_level_qty = 0
                    # locate next lower bid level
                    self._export_level_access(f'LEVEL_ACCESS BID locate_lower {self.bid_max_level_price} //levelDequeue:find next side level')
                    for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
                        if p<self.bid_max_level_price:
                            self.bid_max_level_price = p
                            self.bid_max_level_qty = l.qty
                            break

                    # 修改卖方价格笼子参考价
                    if self.bid_max_level_qty!=0:                       # 买方还有下一档
                        self.ask_cage_ref_px = self.bid_max_level_price
                    else:
                        self._export_level_access(f'LEVEL_ACCESS ASK locate {price} //levelDequeue:update oppo ref px')
                        if price in self.ask_level_tree:             # 卖方本价位有量(此时ask_min_level_price可能是旧的)
                            self.ask_cage_ref_px = price                    #TODO: 卖方hold?
                        elif self.ask_min_level_qty!=0:
                            self.ask_cage_ref_px = self.ask_min_level_price
                        else:
                            self.ask_cage_ref_px = self.LastPx # 一旦lastPx被更新，总会到这里，而此后就不会再用PreClosePx了
                    self.DBG(f'Ask cage ref px={self.ask_cage_ref_px}')
                    
                    if self.TradingPhaseMarket==axsbe_base.TPM.AMTrading or self.TradingPhaseMarket==axsbe_base.TPM.PMTrading:
                        self.ask_waiting_for_cage = True if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM else False
                    else:
                        self.ask_waiting_for_cage = False
                
                #remove要在locate_lower之后
                self.bid_level_tree.pop(price)
                self._export_level_access(f'LEVEL_ACCESS BID remove {price} //levelDequeue')
            else:
                self._export_level_access(f'LEVEL_ACCESS BID writeback {price} //levelDequeue')

        else:## side == SIDE.ASK:
            self.ask_level_tree[price].qty -= qty
            self._export_level_access(f'LEVEL_ACCESS ASK locate {price} //levelDequeue')
            # self.ask_level_tree[price].ts.remove(applSeqNum)
            if price==self.ask_min_level_price:
                self.ask_min_level_qty -= qty

            if (self.ask_cage_lower_ex_max_level_qty==0 or price>self.ask_cage_lower_ex_max_level_price):
                if self.TradingPhaseMarket==axsbe_base.TPM.OpenCall and price>self.PrevClosePx*CYB_ORDER_ENVALUE_MAX_RATE: #从深交所数据上看，超过昨收(新股时为上市价)若干倍的委托不会参与统计
                    self.AskWeightSizeEx -= qty
                    self.AskWeightValueEx -= price * qty
                else:
                    self.AskWeightSize -= qty
                    self.AskWeightValue -= price * qty
            elif self.ask_cage_lower_ex_max_level_qty and price==self.ask_cage_lower_ex_max_level_price:
                self.ask_cage_lower_ex_max_level_qty -= qty
                if self.ask_cage_lower_ex_max_level_qty==0: #卖方价格笼子外最高价被cancel/trade光
                    # locate next high bid level
                    self._export_level_access(f'LEVEL_ACCESS ASK locate_lower {self.ask_cage_lower_ex_max_level_price} //levelDequeue:find next level out of cage')
                    for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历，TODO:可以先判断是否存在
                        if p<self.ask_cage_lower_ex_max_level_price:
                            self.ask_cage_lower_ex_max_level_price = p
                            self.ask_cage_lower_ex_max_level_qty = l.qty
                            self.DBG(f'Refresh ask_cage_lower_ex_max_level_price={self.ask_cage_lower_ex_max_level_price} by canceled/traded all')
                            break


            if self.ask_level_tree[price].qty==0:
                if price==PRICE_MAXIMUM:
                    self.AskWeightPx_uncertain = False #加权价又可确定了

                if price==self.ask_min_level_price:  #卖方最低价被cancel/trade光
                    # locate next higher ask level
                    self.ask_min_level_qty = 0
                    self._export_level_access(f'LEVEL_ACCESS ASK locate_higher {self.ask_min_level_price} //levelDequeue:find next side level')
                    for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                        if p>self.ask_min_level_price:
                            self.ask_min_level_price = p
                            self.ask_min_level_qty = l.qty
                            break

                    # 修改买方价格笼子参考价
                    if self.ask_min_level_qty!=0:                           # 卖方还有下一档
                        self.bid_cage_ref_px = self.ask_min_level_price
                    else:
                        self._export_level_access(f'LEVEL_ACCESS BID locate {price} //levelDequeue:update oppo ref px')
                        if price in self.bid_level_tree:             # 买方本价位有量(此时bid_max_level_price可能是旧的)
                            self.bid_cage_ref_px = price                    #TODO: 买方hold?
                        elif self.bid_max_level_qty!=0:
                            self.bid_cage_ref_px = self.bid_max_level_price
                        else:
                            self.bid_cage_ref_px = self.LastPx # 一旦lastPx被更新，总会到这里，而此后就不会再用PreClosePx了
                    self.DBG(f'Bid cage ref px={self.bid_cage_ref_px}')

                    if self.TradingPhaseMarket==axsbe_base.TPM.AMTrading or self.TradingPhaseMarket==axsbe_base.TPM.PMTrading:
                        self.bid_waiting_for_cage = True if self.market_subtype==MARKET_SUBTYPE.SZSE_STK_GEM else False
                    else:
                        self.bid_waiting_for_cage = False
                
                #remove要在locate_lower之后
                self.ask_level_tree.pop(price)
                self._export_level_access(f'LEVEL_ACCESS ASK remove {price} //levelDequeue')
            else:
                self._export_level_access(f'LEVEL_ACCESS ASK writeback {price} //levelDequeue')


    def onSnap(self, snap:axsbe_snap_stock):
        self.DBG(f'msg#{self.msg_nb} onSnap:{snap}')
        if snap.TradingPhaseSecurity != axsbe_base.TPI.Normal:
            if self.SecurityIDSource==SecurityIDSource_SZSE: #深交所：当天可交易的始终都是可交易
                self.ERR(f'TradingPhaseSecurity={axsbe_base.TPI.str(snap.TradingPhaseSecurity)}@{snap.HHMMSSms}')
                return
            elif self.SecurityIDSource==SecurityIDSource_SSE:#上交所：股票/基金9点14都还是不可交易
                self.INFO(f'TradingPhaseSecurity={axsbe_base.TPI.str(snap.TradingPhaseSecurity)}@{snap.HHMMSSms}')

        ## 更新常量
        if snap.TradingPhaseMarket==axsbe_base.TPM.Starting: # 每天最早的一批快照(7点半前)是没有涨停价、跌停价的，不能只锁一次
            self.constantValue_ready = True
            if self.ChannelNo==CHANNELNO_INIT:
                self.DBG(f"Update constatant: ChannelNo={snap.ChannelNo}, PrevClosePx={snap.PrevClosePx}, UpLimitPx={snap.UpLimitPx}, DnLimitPx={snap.DnLimitPx}")

            self.ChannelNo = snap.ChannelNo
            if self.SecurityIDSource==SecurityIDSource_SZSE:
                if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                    self.PrevClosePx = snap.PrevClosePx // (msg_util.PRICE_SZSE_SNAP_PRECLOSE_PRECISION//PRICE_INTER_STOCK_PRECISION)
                elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                    self.PrevClosePx = snap.PrevClosePx // (msg_util.PRICE_SZSE_SNAP_PRECLOSE_PRECISION//PRICE_INTER_FUND_PRECISION)
                elif self.instrument_type==INSTRUMENT_TYPE.KZZ:
                    self.PrevClosePx = snap.PrevClosePx // (msg_util.PRICE_SZSE_SNAP_PRECLOSE_PRECISION//PRICE_INTER_KZZ_PRECISION)
                else:
                    raise Exception(f'instrument_type={self.instrument_type} is not ready!')    # TODO:
            elif self.SecurityIDSource==SecurityIDSource_SSE:
                if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                    self.PrevClosePx = snap.PrevClosePx // (msg_util.PRICE_SSE_PRECISION//PRICE_INTER_STOCK_PRECISION)
                elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                    self.PrevClosePx = snap.PrevClosePx // (msg_util.PRICE_SSE_PRECISION//PRICE_INTER_FUND_PRECISION)
                elif self.instrument_type==INSTRUMENT_TYPE.BOND:
                    self.PrevClosePx = 0 # 上海债券快照没有带昨收！
                else:
                    raise Exception(f'instrument_type={self.instrument_type} is not ready!')    #
            else:
                raise Exception(f'SecurityIDSource={self.SecurityIDSource} is not ready!')    # TODO:

            if self.SecurityIDSource==SecurityIDSource_SZSE:
                self.ask_cage_ref_px = self.PrevClosePx
                self.bid_cage_ref_px = self.PrevClosePx
                self.DBG(f'Init Bid cage ref px={self.bid_cage_ref_px}')

                self.UpLimitPx = snap.UpLimitPx
                self.DnLimitPx = snap.DnLimitPx
                
                if self.SecurityIDSource==SecurityIDSource_SZSE:
                    if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                        self.UpLimitPrice = snap.UpLimitPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_STOCK_PRECISION)
                        self.DnLimitPrice = snap.DnLimitPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_STOCK_PRECISION)
                    elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                        self.UpLimitPrice = snap.UpLimitPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_FUND_PRECISION)
                        self.DnLimitPrice = snap.DnLimitPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_FUND_PRECISION)
                    elif self.instrument_type==INSTRUMENT_TYPE.KZZ:
                        self.UpLimitPrice = snap.UpLimitPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_KZZ_PRECISION)
                        self.DnLimitPrice = snap.DnLimitPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_KZZ_PRECISION)
                    else:
                        raise Exception(f'instrument_type={self.instrument_type} is not ready!')    # TODO:
            elif self.SecurityIDSource==SecurityIDSource_SSE:
                pass
            else:
                raise Exception(f'SecurityIDSource={self.SecurityIDSource} is not ready!')    # TODO:

            if self.SecurityIDSource==SecurityIDSource_SZSE:
                self.YYMMDD = snap.TransactTime // SZSE_TICK_CUT # 深交所带日期
            else:
                self.YYMMDD = 0                               # 上交所不带日期

        if self.TradingPhaseMarket==axsbe_base.TPM.Ending and snap.TradingPhaseMarket==axsbe_base.TPM.Ending and not self.closePx_ready:
            if self.SecurityIDSource==SecurityIDSource_SZSE:
                if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                    self.LastPx = snap.LastPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_STOCK_PRECISION)
                elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                    self.LastPx = snap.LastPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_FUND_PRECISION)
                elif self.instrument_type==INSTRUMENT_TYPE.KZZ:
                    self.LastPx = snap.LastPx // (msg_util.PRICE_SZSE_SNAP_PRECISION//PRICE_INTER_KZZ_PRECISION)
                else:
                    pass    # TODO:
            else:
                self.ERR('SSE ClosePx not checked!')

            self.closePx_ready = True
            self.genSnap()

        if snap.TradingPhaseMarket==axsbe_base.TPM.VolatilityBreaking and self.TradingPhaseMarket!=axsbe_base.TPM.VolatilityBreaking:  #进入波动性中断
            self.WARN(f'Enter VolatilityBreaking @{snap.TransactTime}')
            # self.VolatilityBreaking_end_tick = 0
            self.TradingPhaseMarket = axsbe_base.TPM.VolatilityBreaking
            self.genSnap()

        ## 检查重建算法，仅用于测试算法是否正确：
        snap._seq = self.msg_nb
        if (self.SecurityIDSource==SecurityIDSource_SZSE and snap.TradingPhaseMarket<axsbe_base.TPM.OpenCall) \
         or(self.SecurityIDSource==SecurityIDSource_SSE and snap.TradingPhaseMarket<axsbe_base.TPM.PreTradingBreaking):
            # 深交所: 从开盘集合竞价开始生成快照，之前的不记录
            # 上交所：从开盘集合竞价后休市开始生成快照，之前的不记录
            pass
        else:
            # 在重建的快照中检索是否有相同的快照
            if self.last_snap and snap.is_same(self.last_snap) and self._chkSnapTimestamp(snap, self.last_snap):
                self.DBG(f'market snap #{self.msg_nb}({snap.TransactTime})'+
                          f' matches last rebuilt snap #{self.last_snap._seq}({self.last_snap.TransactTime})')
                ks = list(self.rebuilt_snaps.keys())
                for k in ks:
                    if k < snap.NumTrades:
                        self.rebuilt_snaps.pop(k)
                #这里不丢弃last_snap，因为可能无逐笔数据而导致快照不更新
            else:
                matched = False
                if snap.NumTrades in self.rebuilt_snaps:
                    for gen in self.rebuilt_snaps[snap.NumTrades]:
                        if snap.is_same(gen) and self._chkSnapTimestamp(snap, gen):
                            self.DBG(f'market snap #{self.msg_nb}({snap.TransactTime})'+
                                    f' matches history rebuilt snap #{gen._seq}({gen.TransactTime})')
                            matched = True
                            break
                
                if matched:
                    ks = list(self.rebuilt_snaps.keys())
                    for k in ks:
                        if k < snap.NumTrades:
                            self.rebuilt_snaps.pop(k)
                else:
                    if snap.NumTrades not in self.market_snaps:
                        self.market_snaps[snap.NumTrades] = [snap]
                    else:
                        self.market_snaps[snap.NumTrades].append(snap) #缓存交易所快照
                    # self.WARN(f'market snap #{self.msg_nb}({snap.TransactTime}) not found in history rebuilt snaps!')


    def genSnap(self):
        # 在生成快照前，必须确保没有“缓存中待处理”的订单
        # 如果olding_nb > 0, 说明你预期马上会收到“逐笔成交”消息。此时订单簿处于“中间态”（不稳定），不应该生成快照
        # 波动性中断）期间，交易暂停，可能会有订单挂在队列里无法成交，此时允许生成快照。
        assert self.TradingPhaseMarket==axsbe_base.TPM.VolatilityBreaking or self.holding_nb==0, f'{self.SecurityID:06d} genSnap but with holding'

        snap = None
        if self.TradingPhaseMarket < axsbe_base.TPM.OpenCall or self.TradingPhaseMarket > axsbe_base.TPM.Ending:
            # 无需生成
            pass
        elif self.TradingPhaseMarket==axsbe_base.TPM.OpenCall or self.TradingPhaseMarket==axsbe_base.TPM.CloseCall:
            # 集合竞价阶段（9:15-9:25, 14:57-15:00）
            # 调用 genCallSnap，内部包含“虚拟撮合”逻辑，计算参考成交价和虚拟买卖盘
            snap = self.genCallSnap()
        elif self.TradingPhaseMarket==axsbe_base.TPM.VolatilityBreaking:
            # 波动性中断（临时停牌）
            # 调用 genTradingSnap，但标记 isVolatilityBreaking=True，通常意味着不显示买卖盘或只显示特定状态
            snap = self.genTradingSnap(isVolatilityBreaking=True)
        elif self.TradingPhaseMarket==axsbe_base.TPM.Ending:
            # 闭市后（15:00之后）
            # 只有当收盘价计算完成（closePx_ready）后才生成最后一张快照
            if self.closePx_ready: #收盘价已经ready
                snap = self.genTradingSnap()
        else:
            # 连续竞价阶段（9:30-11:30, 13:00-14:57）
            # 调用 genTradingSnap，直接提取买卖队列的前10档生成快照
            snap = self.genTradingSnap()

        if snap is not None:
            # 标记卖方加权平均价是否可信（例如当有价格越界的订单导致加权价溢出时）。
            snap.AskWeightPx_uncertain = self.AskWeightPx_uncertain
            # 对快照中的数值进行钳位处理（防止整数溢出，适应 SBE 协议的字段限制）。
            self._clipSnap(snap)

        ## 调试数据，仅用于测试算法是否正确：
        if snap is not None:
            self.DBG(snap)

            # 连续竞价期间（上午或下午），卖一价必须严格大于买一价
            # 如果出现 Bid >= Ask，说明有可以成交的订单没有成交，这意味着之前的逻辑（如 onLimitOrder 或 onExec）有漏网之鱼，模型状态错误。
            if (snap.TradingPhaseMarket==axsbe_base.TPM.AMTrading or snap.TradingPhaseMarket==axsbe_base.TPM.PMTrading) and\
                len(snap.ask)>0 and snap.ask[0].Qty and len(snap.bid) and snap.bid[0].Qty:
                assert snap.ask[0].Price>snap.bid[0].Price, f'{self.SecurityID:06d} bid.max({snap.bid[0].Price})/ask.min({snap.ask[0].Price}) NG'

            snap._seq = self.msg_nb # 用于调试
            self.last_snap = snap

            #在收到的交易所快照中查找是否有一样的,允许匹配多个快照
            matched = []
            if snap.NumTrades in self.market_snaps:
                for rcv in self.market_snaps[snap.NumTrades]:
                    if snap.is_same(rcv) and self._chkSnapTimestamp(rcv, snap):
                        self.WARN(f'rebuilt snap #{snap._seq}({snap.TransactTime}) matches history market snap #{rcv._seq}({rcv.TransactTime})') # 重建快照在市场快照之后，属于警告
                        matched.append(rcv)
            
            if len(matched): 
                for rcv in matched:
                    self.market_snaps[snap.NumTrades].remove(rcv)    #丢弃已匹配的
                if len(self.market_snaps[snap.NumTrades])==0:
                    self.market_snaps.pop(snap.NumTrades)

            # 总是缓存生成的快照，因为可能要跟多个市场快照匹配
            if snap.NumTrades not in self.rebuilt_snaps:
                self.rebuilt_snaps[snap.NumTrades] = [snap]
            else:
                self.rebuilt_snaps[snap.NumTrades].append(snap)


    def _setSnapFixParam(self, snap):
        '''固定参数:每日开盘集合竞价前确定'''
        snap.SecurityID = self.SecurityID
        if self.SecurityIDSource==SecurityIDSource_SZSE:
            if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                snap.PrevClosePx = self.PrevClosePx * (msg_util.PRICE_SZSE_SNAP_PRECLOSE_PRECISION//PRICE_INTER_STOCK_PRECISION)
            elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                snap.PrevClosePx = self.PrevClosePx * (msg_util.PRICE_SZSE_SNAP_PRECLOSE_PRECISION//PRICE_INTER_FUND_PRECISION)
            elif self.instrument_type==INSTRUMENT_TYPE.KZZ:
                snap.PrevClosePx = self.PrevClosePx * (msg_util.PRICE_SZSE_SNAP_PRECLOSE_PRECISION//PRICE_INTER_KZZ_PRECISION)
            else:
                snap.PrevClosePx = self.PrevClosePx    #TODO:
        else:
            '''TODO-SSE'''
            
        snap.UpLimitPx = self.UpLimitPx
        snap.DnLimitPx = self.DnLimitPx
        snap.ChannelNo = self.ChannelNo

    def _clipSnap(self, snap):
        '''超大数据钳位'''
        snap.AskWeightPx = self._clipInt32(snap.AskWeightPx) #当委托价无上限时，加权价格可能超出32位整数，也没有什么意义了，直接钳位到最大


    # 每次处理 on_msg之前 就调用这个, 更新
    def _useTimestamp(self, TransactTime):
        if self.SecurityIDSource == SecurityIDSource_SZSE:
            # TransactTime 20220426092044460  -- > 9204446
            self.current_inc_tick = TransactTime // SZSE_TICK_MS_TAIL % (SZSE_TICK_CUT // SZSE_TICK_MS_TAIL)    #只用逐笔 (10ms精度) 15000000 24b
        else:
            self.current_inc_tick = TransactTime # 上交所(1ms精度) 150000000
        if self.current_inc_tick >= (1<<TIMESTAMP_BIT_SIZE):
            self.ERR(f'msg.TransactTime={TransactTime} ovf!')


    def _setSnapTimestamp(self, snap):
        if self.SecurityIDSource==SecurityIDSource_SZSE:
            snap.TransactTime = self.YYMMDD * SZSE_TICK_CUT + (self.current_inc_tick*SZSE_TICK_MS_TAIL) #深交所显示精度到ms，多补1位
        elif self.SecurityIDSource==SecurityIDSource_SZSE:
            if self.instrument_type==INSTRUMENT_TYPE.BOND or self.instrument_type==INSTRUMENT_TYPE.KZZ or self.instrument_type==INSTRUMENT_TYPE.NHG:
                snap.TransactTime = self.current_inc_tick #债券精确到ms
            else:
                snap.TransactTime = self.current_inc_tick // 100 #上交所只显示到秒，去掉10ms和100ms两位



    def genCallSnap(self, show_level_nb=10, show_potential=False):
        '''
        show_level_nb:  展示的价格档数
        show_potential: 在无法撮合时展示出双方价格档
        '''
        # if self.msg_nb>=885:
        #    self._print_levels()
        #1. 查找 最低卖出价格档、最高买入价格档
        _bid_max_level_price = self.bid_max_level_price  # 买方下一档价
        _bid_max_level_qty = self.bid_max_level_qty      # 买方下一档量
        _ask_min_level_price = self.ask_min_level_price  # 卖方下一档价
        _ask_min_level_qty = self.ask_min_level_qty      # 卖方下一档量

        # 2. 初始参考成交价 (Reference Price)
        # 如果两边都空，价格为0
        # 如果一边空，取另一边价格
        # 如果都有，暂时设为0（后面算）
        if _bid_max_level_qty==0 and _ask_min_level_qty==0: #两边都无委托
            price = 0
        else: # 至少一边存在委托
            if _bid_max_level_qty==0:
                price = _ask_min_level_price
            elif _ask_min_level_qty==0:
                price = _bid_max_level_price
            else:   #两边都存在，双方最优价可能交叉也可能无交叉
                price = 0

        
        #3. 初始 总成交数量 = 0
        volumeTrade = 0  # 累计虚拟成交量（这是我们要计算的核心指标）
        bid_Qty = 0      # 当前档位剩余未匹配买量
        ask_Qty = 0      # 当前档位剩余未匹配卖量
        bid_trade_level_nb = 0
        ask_trade_level_nb = 0
        
        
        # 如果昨天有收盘价就用昨收，有最近成交就用最近成交。这是用来在价格有分歧时“定锚”用的
        _ref_px = self.PrevClosePx if self.NumTrades==0 else self.LastPx

        #4. 撮合循环：
        while True:  # 
            # 条件：双方都有量，且买一价 >= 卖一价 (可以成交)
            if _bid_max_level_qty!=0 and _ask_min_level_qty!=0 and _bid_max_level_price >= _ask_min_level_price:    # 双方均有最优委托 且 双方最优价有交叉
                # 加载当前档位的量（如果之前没加载过）
                if bid_Qty == 0:
                    bid_Qty = _bid_max_level_qty
                if ask_Qty == 0:
                    ask_Qty = _ask_min_level_qty
                
                # 这部分模拟了“吃单”过程，原则是成交量取决于量小的一方。
                # 买量 大于等于 卖量
                if bid_Qty >= ask_Qty:
                    volumeTrade += ask_Qty # 1. 累计成交量：加上较小的卖方量
                    bid_Qty -= ask_Qty     # 2. 买方剩余：减去已成交的量
                    ask_Qty = 0            # 3. 卖方耗尽：当前卖一档全部吃光

                    ask_trade_level_nb += 1 # 统计：卖方又成交了一个档位
                    if bid_Qty==0:          # 特例：如果买也耗尽了
                        bid_trade_level_nb += 1
                else:
                    # 买量 小于 卖量
                    volumeTrade += bid_Qty   # 1. 累计成交量：加上较小的买方量
                    ask_Qty -= bid_Qty       # 2. 卖方剩余
                    bid_Qty = 0              # 3. 买方耗尽

                    bid_trade_level_nb += 1  # 统计档位

                # 上面的 if else 操作后，必然有一方（或双方）的 Qty 变为 0，意味着需要移动价格指针到下一档


                # 当买卖双方在当前重叠区间的量完全相等时，意味着在这个节点，价格是不确定的（在该区间内任意价格都能让这笔量成交）
                if bid_Qty == 0 and ask_Qty == 0:   # 双方都为0了
                    # 基准价在买一和卖一之间，直接取基准价
                    if _bid_max_level_price>=_ref_px and _ask_min_level_price<=_ref_px:   #
                        price = _ref_px
                    else:
                        # 基准价在区间外，取离基准价最近的那个边界
                        if abs(_bid_max_level_price-_ref_px) < abs(_ask_min_level_price-_ref_px):
                            price = _bid_max_level_price
                        else:
                            price = _ask_min_level_price

                # 买方指针移动 (Locate Next Bid)
                # 如果买方当前档位被吃光了（bid_Qty == 0），需要寻找下一个更低的买单。
                if bid_Qty == 0:
                    # 关键点：如果买方没了但卖方还有，成交价暂时锚定在卖方价
                    if ask_Qty != 0:
                        price = _ask_min_level_price

                    # locate next lower bid level
                    # 下面开始寻找下一档
                    _bid_max_level_qty = 0  # 清空旧量
                    # self._export_level_access(f'LEVEL_ACCESS BID locate_lower {_bid_max_level_price} //callSnap:next side level')
                    # 遍历买单树（从高到低）
                    for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):
                        if p<_bid_max_level_price: # 找到第一个比当前价格低的价格
                            # if price<=p:
                            #     price = p+1
                            _bid_max_level_price = p   # 更新买一价指针
                            _bid_max_level_qty = l.qty # 更新买一量指针
                            break                           # 找到即停止，准备下一轮撮合

                if ask_Qty == 0:
                    if bid_Qty != 0:
                        price = _bid_max_level_price  # 如果卖方没了买方还有，价格锚定在买方价
                    # locate next higher ask level
                    _ask_min_level_qty = 0
                    # self._export_level_access(f'LEVEL_ACCESS ASK locate_higher {_ask_min_level_price} //callSnap:next side level')
                    for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                        if p>_ask_min_level_price:     # 找到第一个比当前价格高的价格
                            # if price>=p:
                            #     price = p-1
                            _ask_min_level_price = p   # 更新卖一价指针
                            _ask_min_level_qty = l.qty # 更新卖一量指针
                            break

            else:   #后续买卖双方至少一方无委托，或价格无交叉, （买一价 < 卖一价，不再有交叉）
                
                # 完美匹配场景：上一轮撮合双方量刚好耗尽
                # 如果有一方还有剩余量（例如 bid_Qty > 0），那么成交价必然锁定在那个档位，不需要修正。只有当双方都耗尽，价格处于“真空地带”时，才需要检查之前定的 price 是否合理。

                """
                    这段代码是集合竞价撮合算法中处理**“价格分歧”与“最小剩余量”**原则的核心实现。
                    它处理的是一种极其特殊的临界状态：完美匹配（Perfect Match）后，买卖双方剩下的第一档价格紧紧相邻（只差 1 分钱/1个Tick），且之前的定价逻辑（通常是基于基准价）可能导致了逻辑冲突或不满足最优规则。
                    以下是深度解析：
                    1. 代码干了什么？（操作层面）
                        在进入这个 else 分支前，场景如下：
                            状态：上一轮撮合双方筹码刚好全部抵消（ask_Qty==0 且 bid_Qty==0）。
                            位置：买方剩的最高价（例如 10.00）和卖方剩的最低价（例如 10.01）只差 1 个 tick。
                            触发：初步估算的 price 偏向了卖方（例如 10.01），需要决策最终到底定在 10.00 还是 10.01。
                    代码执行了以下“二选一”的逻辑：
                        1. 比较剩余盘口量： 对比 卖一剩单量 (_ask_min_level_qty) 和 买一剩单量 (_bid_max_level_qty)。
                        2. 选择量小的一方

深度解读：
场景模拟： 假设撮合到最后，中间空出来了。
买一剩：10.00元，有 1000手。
卖一剩：10.01元，有 10手。
基准价：10.05元（偏高）。
冲突： 按照基准价原则，开盘价想往高了靠（10.01）。但如果定在 10.01：
10.00 的买单显然不能成交（价格太低）。
10.01 的卖单是可以成交的（价格匹配）。
但是，因为之前是“完美匹配”，说明之前的量都消化了。现在定在 10.01，意味着这 10手 卖单虽然价格匹配，但没对手盘了（因为买方最高才 10.00）。
此时，市场上的“未成交剩余量”就是这 10手。
反之，如果定在 10.00：
10.00 的买单可以成交。
同样因为没对手盘（卖方最低 10.01），这 1000手 买单全部无法成交。
此时，市场上的“未成交剩余量”就是 1000手。
决策： 交易所规则要求剩余量最小。
方案 A（定 10.01）：剩余 10 手。
方案 B（定 10.00）：剩余 1000 手。
结论：选择 方案 A。尽管 10.01 离买方心理价位远，但它留下的市场不平衡（Imbalance）最小。
                """
                if ask_Qty==0 and bid_Qty==0:   # 双方恰好成交，根据下一档价格，可能需要修正成交价
                    
                    # 初步估算的 price >= 卖方下一档价格（_ask_min_level_price）。
                    # 如果又卖一量, 并且 价格 大于等于 卖一价,   那他穿透了卖1变成了 卖2或者3了
                    # 如果成交价真的这么高，卖方下一档的订单理应成交，但它实际上没成交（因为循环退出了）。说明价格定高了，必须下调
                    if _ask_min_level_qty and price>=_ask_min_level_price: #成交价高于卖方下一档，必须修正到小于等于卖方下一档

                        # 买方没单了，或者买方下一档价格 + 1个tick < 卖方下一档价格。说明买卖中间有缝隙
                        # 直接把价格修到 卖一价 - 1。这是最安全的“天花板”价格，既不触发卖方成交，又尽可能接近基准价
                        if _bid_max_level_qty==0 or _bid_max_level_price+1<_ask_min_level_price:    # 买方下一档+1分钱 小于 卖方下一档，修到卖方下一档-1
                            price = _ask_min_level_price-1
                        else:
                            # 如果没缝隙, 隐含条件：买方下一档 + 1 == 卖方下一档（仅差1个tick
                            # 必须二选一：要么定在卖方价，要么定在买方价
                            # 当买卖盘口紧邻（如买 10.00，卖 10.01），而计算出的价格需要修正时，通常遵循**“最小剩余量原则”**。
                            # 如果卖方量更小，就让价格定在卖方价（10.01），并显示卖方剩余量
                            # 这模拟了一种“试图吃掉卖方但没吃完”的状态，在快照上揭示出更小的 Imbalance（不平衡量），符合撮合原则
                            if _ask_min_level_qty <= _bid_max_level_qty:   # 卖方双方下一档只差一分钱，选量小的，同量卖方优先
                                # 卖方量小
                                price = _ask_min_level_price
                                ask_Qty = _ask_min_level_qty  # 关键：将卖方量作为“未匹配量”揭示
                            else:
                                price = _bid_max_level_price
                                bid_Qty = _bid_max_level_qty  # 关键：将买方量作为“未匹配量”揭示

                    elif _bid_max_level_qty and price<=_bid_max_level_price: #成交价低于买方下一档，必须修正到大于等于买方下一档
                        if _bid_max_level_qty==0 or _ask_min_level_price>_bid_max_level_price+1: # 卖方下一档分钱 大于 买方下一档+1，修到买方下一档+1
                            price = _bid_max_level_price+1
                        else:
                            if _bid_max_level_qty <= _ask_min_level_qty: # 卖方双方下一档只差一分钱，选量小的，同量买方优先
                                price = _bid_max_level_price
                                bid_Qty = _bid_max_level_qty
                            else:
                                price = _ask_min_level_price
                                ask_Qty = _ask_min_level_qty
                   
                break


        ## 集中竞价期间不需要统计成交信息(TotalVolumeTrade & TotalValueTrade)

        # price 小数位数扩展
        price = self._fmtPrice_inter2snap(price)

        # 价格档
        snap_ask_levels = {}
        snap_bid_levels = {}
        if volumeTrade == 0: # 无法撮合时
            if not show_potential:
                for i in range(0, show_level_nb):
                    snap_ask_levels[i] = price_level(0,0)
                    snap_bid_levels[i] = price_level(0,0)
            else:   #无法撮合时，揭示多档
                snap_ask_levels, snap_bid_levels = self._getLevels(show_level_nb)
        else: #可撮合时，揭示2档
            # 意思就是 定在price 成交了 volumeTrade 手, 未成交ask_Qty 手, 告诉市场，目前大家公认的“开盘价”可能是多少，以及在这个价格能成多少手
            # 2档 揭示卖方剩余未匹配量
            snap_ask_levels[0] = price_level(price, volumeTrade)  
            snap_ask_levels[1] = price_level(0, ask_Qty)

            # 将卖三（Ask 3）到卖十（Ask 10）全部清零。
            for i in range(2, show_level_nb):
                snap_ask_levels[i] = price_level(0,0)

            snap_bid_levels[0] = price_level(price, volumeTrade)
            snap_bid_levels[1] = price_level(0, bid_Qty)
            
            for i in range(2, show_level_nb):
                snap_bid_levels[i] = price_level(0,0)


        #### 开始构造快照
        if self.SecurityIDSource==SecurityIDSource_SZSE:
            if self.instrument_type==INSTRUMENT_TYPE.STOCK or self.instrument_type==INSTRUMENT_TYPE.KZZ:
                snap_call = axsbe_snap_stock(SecurityIDSource=self.SecurityIDSource, source=f"AXOB-call")
            else:
                raise Exception(f'genCallSnap for instrument_type={self.instrument_type} is not ready!')
        elif self.SecurityIDSource==SecurityIDSource_SSE:
            if self.instrument_type==INSTRUMENT_TYPE.BOND or self.instrument_type==INSTRUMENT_TYPE.KZZ or self.instrument_type==INSTRUMENT_TYPE.NHG:
                snap_call = axsbe_snap_stock(SecurityIDSource=self.SecurityIDSource, MsgType=MsgType_exe_sse_bond, source=f"AXOB-call")
            else:
                raise Exception(f'genCallSnap for instrument_type={self.instrument_type} is not ready!')
        
        # 设置固定参数（如代码、涨跌停价等）
        self._setSnapFixParam(snap_call)

        ## 本地维护参数
        snap_call.ask = snap_ask_levels
        snap_call.bid = snap_bid_levels
		# 以下参数开盘集合竞价期间为0，收盘集合竞价期间有值
        snap_call.NumTrades = self.NumTrades
        snap_call.TotalVolumeTrade = self.TotalVolumeTrade
        snap_call.TotalValueTrade = self.TotalValueTrade
        snap_call.LastPx = self._fmtPrice_inter2snap(self.LastPx)
        snap_call.HighPx = self._fmtPrice_inter2snap(self.HighPx)
        snap_call.LowPx = self._fmtPrice_inter2snap(self.LowPx)
        snap_call.OpenPx = self._fmtPrice_inter2snap(self.OpenPx)
        

        # 本地维护参数
        if self.SecurityIDSource==SecurityIDSource_SZSE:
            snap_call.BidWeightPx = 0   #开盘撮合时期为0
            snap_call.BidWeightSize = 0
            snap_call.AskWeightPx = 0
            snap_call.AskWeightSize = 0
        elif self.SecurityIDSource==SecurityIDSource_SSE:
            if self.BidWeightSize != 0:
                snap_call.BidWeightPx = (int((self.BidWeightValue<<1) / self.BidWeightSize) + 1) >> 1 # 四舍五入
                snap_call.BidWeightPx = self._fmtPrice_inter2snap(snap_call.BidWeightPx)
            else:
                snap_call.BidWeightPx = 0
            snap_call.BidWeightSize = self.BidWeightSize
            
            if self.AskWeightSize != 0:
                snap_call.AskWeightPx = (int((self.AskWeightValue<<1) / self.AskWeightSize) + 1) >> 1 # 四舍五入
                snap_call.AskWeightPx = self._fmtPrice_inter2snap(snap_call.AskWeightPx)
            else:
                snap_call.AskWeightPx = 0
            snap_call.AskWeightSize = self.AskWeightSize

        #最新的一个逐笔消息时戳
        self._setSnapTimestamp(snap_call)

        snap_call.update_TradingPhaseCode(self.TradingPhaseMarket, axsbe_base.TPI.Normal)

        return snap_call
        

    def genTradingSnap(self, isVolatilityBreaking=False, level_nb=10):
        '''
        生成连续竞价期间快照
        level_nb: 快照单边档数
        '''
        snap_bid_levels = {}
        lv = 0
        if not isVolatilityBreaking: #临停期间，各档均填0；非临停期间才从价格档中取值
            # 遍历买方价格树：sorted(..., reverse=True) 表示从大到小排序（买一价最高）
            for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):
                # 【关键逻辑：价格笼子过滤】
                # bid_cage_upper_ex_min_level_qty==0: 表示没有被笼子隐藏的订单
                # p < self.bid_cage_upper_ex_min_level_price: 或者当前价格 p 小于“笼子外最低价”（即 p 在笼子内）
                if self.bid_cage_upper_ex_min_level_qty==0 or p<self.bid_cage_upper_ex_min_level_price:
                    # 创建价格档位对象：将内部价格精度转换为快照精度，并填入数量
                    snap_bid_levels[lv] = price_level(self._fmtPrice_inter2snap(p), l.qty)
                    lv += 1
                    # 如果填满了需要的档位数（如10档），就停止遍历
                    if lv>=level_nb:
                        break
        
        # 如果实际档位不足 10 档（例如只有 5 个买单），剩下的 5-9 档用 (0, 0) 填充
        for i in range(lv, level_nb):
            snap_bid_levels[i] = price_level(0, 0)
            
        snap_ask_levels = {}
        lv = 0
        if not isVolatilityBreaking: #临停期间，各档均填0；非临停期间才从价格档中取值
            # self._export_level_access(f'LEVEL_ACCESS ASK locate_higher {self.ask_min_level_price} x{level_nb} //tradingSnap:traverse side level')
            # # 遍历卖方价格树：sorted(..., reverse=False) 表示从小到大排序（卖一价最低）
            for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                # 【关键逻辑：价格笼子过滤】
                if self.ask_cage_lower_ex_max_level_qty==0 or p>self.ask_cage_lower_ex_max_level_price:
                    snap_ask_levels[lv] = price_level(self._fmtPrice_inter2snap(p), l.qty)
                    lv += 1
                    if lv>=level_nb:
                        break
        # 填充剩余空档
        for i in range(lv, level_nb):
            snap_ask_levels[i] = price_level(0, 0)


        # 根据证券类型（股票或可转债）创建对应的快照对象
        if self.instrument_type==INSTRUMENT_TYPE.STOCK or self.instrument_type==INSTRUMENT_TYPE.KZZ:
            snap = axsbe_snap_stock(SecurityIDSource=self.SecurityIDSource, source=f"AXOB-{level_nb}")
        else:
            self.WARN(f'genTradingSnap for instrument_type={self.instrument_type} is not ready!')
            return None # TODO: not ready [Mid priority]
        
        # 将构建好的买卖盘字典赋值给快照对象
        snap.ask = snap_ask_levels
        snap.bid = snap_bid_levels
        
        # 固定参数
        # 设置固定参数：如证券代码、昨收价、涨跌停价等（这些在开盘前就确定了）
        self._setSnapFixParam(snap)


        # 本地维护参数
        # 设置动态统计参数：从 AXOB 对象的当前状态中拷贝
        snap.NumTrades = self.NumTrades
        snap.TotalVolumeTrade = self.TotalVolumeTrade
        snap.TotalValueTrade = self.TotalValueTrade
        # 设置并转换价格精度（内部高精度 -> 快照显示精度）
        snap.LastPx = self._fmtPrice_inter2snap(self.LastPx)
        snap.HighPx = self._fmtPrice_inter2snap(self.HighPx)
        snap.LowPx = self._fmtPrice_inter2snap(self.LowPx)
        snap.OpenPx = self._fmtPrice_inter2snap(self.OpenPx)
        

        #维护参数
        if isVolatilityBreaking: #临停期间填0
            snap.BidWeightPx = 0
            snap.BidWeightSize = 0
            snap.AskWeightPx = 0
            snap.AskWeightSize = 0
        else:
            # --- 买方加权价计算 ---
            if self.BidWeightSize != 0:
                # round(V/S) ≈ int((V * 2 / S) + 1) / 2,  这里作者纯属显摆
                snap.BidWeightPx = (int((self.BidWeightValue<<1) / self.BidWeightSize) + 1) >> 1 # 四舍五入
                snap.BidWeightPx = self._fmtPrice_inter2snap(snap.BidWeightPx)
            else:
                snap.BidWeightPx = 0
            snap.BidWeightSize = self.BidWeightSize
            
            # --- 卖方加权价计算（逻辑同上）---
            if self.AskWeightSize != 0:
                # round(V/S) ≈ int((V * 2 / S) + 1) / 2
                snap.AskWeightPx = (int((self.AskWeightValue<<1) / self.AskWeightSize) + 1) >> 1 # 四舍五入
                snap.AskWeightPx = self._fmtPrice_inter2snap(snap.AskWeightPx)
            else:
                snap.AskWeightPx = 0
            snap.AskWeightSize = self.AskWeightSize

        # 设置快照的时间戳（通常取最近一笔逐笔消息的时间）
        self._setSnapTimestamp(snap)

        # 设置交易阶段代码（例如 'T' 表示连续竞价，'Normal' 表示正常交易状态）
        snap.update_TradingPhaseCode(self.TradingPhaseMarket, axsbe_base.TPI.Normal)

        return snap

    def _clipInt32(self, x):
        if x>(0x7fffffff):
            return 0x7fffffff
        else:
            return x

    def _clipUint32(self, x):
        if x>(0xffffffff):
            return 0xffffffff
        else:
            return x

    
    def _fmtPrice_inter2snap(self, price):
        # price 小数位数扩展
        if self.SecurityIDSource==SecurityIDSource_SZSE:
            # 深圳快照价格精度6位小数（唯有PrevClosePx是4位小数）
            if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                price *= msg_util.PRICE_SZSE_SNAP_PRECISION // PRICE_INTER_STOCK_PRECISION    # 内部2位，输出6位
            elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                price *= msg_util.PRICE_SZSE_SNAP_PRECISION // PRICE_INTER_FUND_PRECISION    # 内部3位，输出6位
            elif self.instrument_type==INSTRUMENT_TYPE.KZZ:
                price *= msg_util.PRICE_SZSE_SNAP_PRECISION // PRICE_INTER_KZZ_PRECISION    # 内部3位，输出6位
            else:
                price = None
        elif self.SecurityIDSource==SecurityIDSource_SSE:
            # 上海快照价格精度3位小数
            if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                price *= msg_util.PRICE_SSE_PRECISION // PRICE_INTER_STOCK_PRECISION    # 内部2位，输出3位
            elif self.instrument_type==INSTRUMENT_TYPE.FUND:
                price *= msg_util.PRICE_SSE_PRECISION // PRICE_INTER_FUND_PRECISION    # 内部3位，输出3位
            else:
                price = None
        else:
            price = None
        return price

    def _getLevels(self, level_nb):
        '''
        输出：卖方最优n档, 买方最优n档
        '''
        snap_ask_levels = {}
        snap_bid_levels = {}
        
        _bid_max_level_price = self.bid_max_level_price
        _bid_max_level_qty = self.bid_max_level_qty
        _ask_min_level_price = self.ask_min_level_price
        _ask_min_level_qty = self.ask_min_level_qty

        for nb in range(level_nb):
            if _ask_min_level_qty!=0:
                snap_ask_levels[nb] = price_level(self._fmtPrice_inter2snap(_ask_min_level_price), _ask_min_level_qty)
                # locate next higher ask level
                _ask_min_level_qty = 0
                self._export_level_access(f'LEVEL_ACCESS ASK locate_higher {_ask_min_level_price} //snap:traverse side level')
                for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=False):    #从小到大遍历
                    if p>_ask_min_level_price:
                        _ask_min_level_price = p
                        _ask_min_level_qty = l.qty
                        break
            else:
                snap_ask_levels[nb] = price_level(0,0)

            if _bid_max_level_qty!=0:
                snap_bid_levels[nb] = price_level(self._fmtPrice_inter2snap(_bid_max_level_price), _bid_max_level_qty)
                # locate next lower bid level
                _bid_max_level_qty = 0
                self._export_level_access(f'LEVEL_ACCESS BID locate_lower {_bid_max_level_price} //snap:traverse side level')
                for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
                    if p<_bid_max_level_price:
                        _bid_max_level_price = p
                        _bid_max_level_qty = l.qty
                        break
            else:
                snap_bid_levels[nb] = price_level(0,0)

        return snap_ask_levels, snap_bid_levels

    def _chkSnapTimestamp(self, se_snap, ax_snap):
        '''
        return True: 双方时戳合法
        检查交易所快照和本地重建快照的时戳是否符合：
        深交所本地时戳的秒应小于等交易所快照时戳
        '''

        # 休市阶段，忽略时戳检查
        if se_snap.TradingPhaseMarket==ax_snap.TradingPhaseMarket and \
            (se_snap.TradingPhaseMarket==axsbe_base.TPM.PreTradingBreaking or \
             se_snap.TradingPhaseMarket==axsbe_base.TPM.Breaking or \
             se_snap.TradingPhaseMarket>=axsbe_base.TPM.Ending \
            ):
            return True

        se_timestamp = se_snap.TransactTime
        ax_timestamp = ax_snap.TransactTime

        if self.SecurityIDSource==SecurityIDSource_SZSE:
            return ax_timestamp//1000 <= se_timestamp//1000 +1
        elif self.SecurityIDSource==SecurityIDSource_SSE:
            '''TODO-SSE'''
        else:
            return False

    def are_you_ok(self):
        im_ok = True
        if len(self.market_snaps):
            self.ERR(f'unmatched market snap size={len(self.market_snaps)}:')
            n = 0
            for s,ls in self.market_snaps.items():
                self.ERR(f'\tNumTrades={s}')
                for ss in ls:
                    self.ERR(f'\t\t#{ss._seq}\t@{ss.TransactTime}')
                n += 1
                if n>=3:
                    self.ERR("\t......")
                    break
            im_ok = False
        return im_ok

    @property
    def order_map_size(self):
        return len(self.order_map)

    @property
    def level_tree_size(self):
        return len(self.bid_level_tree) + len(self.ask_level_tree)

    @property
    def bid_level_tree_size(self):
        return len(self.bid_level_tree)

    @property
    def ask_level_tree_size(self):
        return len(self.ask_level_tree)

    def profile(self):
        if self.order_map_size>self.pf_order_map_maxSize: self.pf_order_map_maxSize = self.order_map_size
        if self.level_tree_size>self.pf_level_tree_maxSize: self.pf_level_tree_maxSize = self.level_tree_size
        if self.bid_level_tree_size>self.pf_bid_level_tree_maxSize: self.pf_bid_level_tree_maxSize = self.bid_level_tree_size
        if self.ask_level_tree_size>self.pf_ask_level_tree_maxSize: self.pf_ask_level_tree_maxSize = self.ask_level_tree_size
        if self.AskWeightSize>self.pf_AskWeightSize_max: self.pf_AskWeightSize_max = self.AskWeightSize
        if self.AskWeightValue>self.pf_AskWeightValue_max: self.pf_AskWeightValue_max = self.AskWeightValue
        if self.BidWeightSize>self.pf_BidWeightSize_max: self.pf_BidWeightSize_max = self.BidWeightSize
        if self.BidWeightValue>self.pf_BidWeightValue_max: self.pf_BidWeightValue_max = self.BidWeightValue

    def _describe_px(self, p):
        s = ''
        if p==self.bid_max_level_price:
            s += '\tbid_max'
        if p==self.ask_min_level_price:
            s += '\task_min'
        if p==self.ask_cage_ref_px:
            s += '\task_cage_ref'
        if p==self.bid_cage_ref_px:
            s += '\tbid_cage_ref'
        if self.ask_cage_lower_ex_max_level_qty and p==self.ask_cage_lower_ex_max_level_price:
            s += '\task_cage_lower_ex_max'
        if self.bid_cage_upper_ex_min_level_qty and p==self.bid_cage_upper_ex_min_level_price:
            s += '\tbid_cage_upper_ex_min'
        return s

    def _print_levels(self):
        for p, l in sorted(self.ask_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
            s = f'ask\t{l}{self._describe_px(l.price)}'
            self.DBG(s)
        for p, l in sorted(self.bid_level_tree.items(),key=lambda x:x[0], reverse=True):    #从大到小遍历
            s = f'bid\t{l}{self._describe_px(l.price)}'
            self.DBG(s)

    def _export_level_access(self, msg):
        if EXPORT_LEVEL_ACCESS:
            self.DBG(msg)

    def __str__(self) -> str:
        s = f'axob-behave {self.SecurityID:06d} {self.YYMMDD}-{self.current_inc_tick} msg_nb={self.msg_nb}\n'
        s+= f'  order_map={len(self.order_map)} bid_level_tree={len(self.bid_level_tree)} ask_level_tree={len(self.ask_level_tree)}\n'
        s+= f'  bid_max_level_price={self.bid_max_level_price} bid_max_level_qty={self.bid_max_level_qty}\n'
        s+= f'  ask_min_level_price={self.ask_min_level_price} ask_min_level_qty={self.ask_min_level_qty}\n'
        s+= f'  rebuilt_snaps={len(self.rebuilt_snaps)} market_snaps={len(self.market_snaps)}\n'
        s+= '\n'
        s+= f'  pf_order_map_maxSize={self.pf_order_map_maxSize}({bitSizeOf(self.pf_order_map_maxSize)}b)\n'
        s+= f'  pf_level_tree_maxSize={self.pf_level_tree_maxSize}({bitSizeOf(self.pf_level_tree_maxSize)}b)\n'
        s+= f'  pf_bid_level_tree_maxSize={self.pf_bid_level_tree_maxSize}({bitSizeOf(self.pf_bid_level_tree_maxSize)}b) pf_ask_level_tree_maxSize={self.pf_ask_level_tree_maxSize}({bitSizeOf(self.pf_ask_level_tree_maxSize)}b)\n'
        s+= f'  pf_AskWeightSize_max={self.pf_AskWeightSize_max}({bitSizeOf(self.pf_AskWeightSize_max)}b)\n'
        s+= f'  pf_AskWeightValue_max={self.pf_AskWeightValue_max}({bitSizeOf(self.pf_AskWeightValue_max)}b)\n'
        s+= f'  pf_BidWeightSize_max={self.pf_BidWeightSize_max}({bitSizeOf(self.pf_BidWeightSize_max)}b)\n'
        s+= f'  pf_BidWeightValue_max={self.pf_BidWeightValue_max}({bitSizeOf(self.pf_BidWeightValue_max)}b)\n'

        return s

    def save(self):
        '''save/load 用于保存/加载测试时刻'''
        data = {}
        for attr in self.__slots__:
            if attr in ['logger', 'DBG', 'INFO', 'WARN', 'ERR']:
                continue

            value = getattr(self, attr)
            if attr in ['order_map', 'bid_level_tree', 'ask_level_tree']:
                data[attr] = {}
                for i in value:
                    data[attr][i] = value[i].save()
            elif attr == 'rebuilt_snaps' or attr == 'market_snaps':
                data[attr] = {}
                for i in value:
                    data[attr][i] = [x.save() for x in value[i]]
            elif attr == 'last_snap':
                if value is None:
                    data[attr] = None
                else:
                    data[attr] = value.save()
            else:
                data[attr] = value
        return data

    def load(self, data):
        setattr(self, 'instrument_type', data['instrument_type'])
        for attr in self.__slots__:
            if attr in ['logger', 'DBG', 'INFO', 'WARN', 'ERR']:
                continue

            if attr == 'order_map':
                v = {}
                for i in data[attr]:
                    v[i] = ob_order(axsbe_order(), INSTRUMENT_TYPE.UNKNOWN)
                    v[i].load(data[attr][i])
                setattr(self, attr, v)
            elif attr in ['bid_level_tree', 'ask_level_tree']:
                v = {}
                for i in data[attr]:
                    v[i] = level_node(-1, -1, -1)
                    v[i].load(data[attr][i])
                setattr(self, attr, v)
            elif attr == 'rebuilt_snaps' or attr == 'market_snaps':
                v = {}
                for i in data[attr]:
                    vv = []
                    for d in data[attr][i]:
                        if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                            s = axsbe_snap_stock()
                        else:
                            raise f'unable to load instrument_type={self.instrument_type}'
                        s.load(d)
                        vv.append(s)
                    v[i] = vv
                setattr(self, attr, v)
            elif attr == 'last_snap':
                if data[attr] is None:
                    v = None
                else:
                    if self.instrument_type==INSTRUMENT_TYPE.STOCK:
                        v = axsbe_snap_stock()
                    else:
                        raise f'unable to load instrument_type={self.instrument_type}'
                    v.load(data[attr])
                setattr(self, attr, v)
            else:
                setattr(self, attr, data[attr])

            # elif attr in data:
            #     setattr(self, attr, data[attr])
            # else:
            #     print(f'AXOB.{attr} not in load data!')
            #     setattr(self, attr, 0)

        ## 日志
        self.logger = logging.getLogger(f'{self.SecurityID:06d}')
        g_logger = logging.getLogger('main')
        self.logger.setLevel(g_logger.getEffectiveLevel())
        for h in g_logger.handlers:
            self.logger.addHandler(h)
            axob_logger.addHandler(h) #这里补上模块日志的handler，有点ugly TODO: better way [low prioryty]

        self.DBG = self.logger.debug
        self.INFO = self.logger.info
        self.WARN = self.logger.warning
        self.ERR = self.logger.error
