import queue
import threading
from math import floor
from time import sleep, time, localtime

import modbus_tk.defines as cst
from modbus_tk import modbus_tcp

_LOCK = threading.RLock()
_HOOKS = {}
# 虚拟从站中代表真实从站状态的寄存器长度
lenslavestate = 10
# 从站的临时列表，每一IP端口对应一个子列表
IDs = [[2, 3, 4, 5, 6, 7, 8, 9, 10], [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]]
# 形成从站与序号的对应关系，并统计从站个数
indexspace = []
index = 0
i = 0
while i < len(IDs):
    j = 0
    indexspace.append({})
    while j < len(IDs[i]):
        indexspace[i][index] = IDs[i][j]
        index += 1
        j += 1
    i += 1
slaveNumber = index

# 确定从站的寄存器起始地址和数量
CoilsStart = 1
CoilsNumber = 0
HoldingsStart = 1
HoldingsNumber = 11
# 单个真实从站对应的虚拟从站的holding长度
lenperslave = HoldingsNumber + lenslavestate
# 创建共享字典，用于虚拟从站存入写入数据，该字典的键是真实从站的唯一序号，值是一个长度为2的列表，第一项为真实从站的要写入值的起始地址，第二项为要写入的值
valuepoll = {}
i = 0
while i < slaveNumber:
    valuepoll[i] = None
    i += 1


def modbusmaster(q, master, ID=None):
    if ID is None:
        ID = {0: 1, }
    while True:
        for masterslaveindex in ID.keys():
            # 当共享字典的对应部分不为None时，将相应值写入真实从站
            if valuepoll[masterslaveindex]:
                print(valuepoll, masterslaveindex)
                master.execute(ID[masterslaveindex], cst.WRITE_SINGLE_REGISTER,
                               valuepoll[masterslaveindex][0], output_value=valuepoll[masterslaveindex][1])
                valuepoll[masterslaveindex] = None
            readResult = master.execute(ID[masterslaveindex], cst.READ_HOLDING_REGISTERS, HoldingsStart, HoldingsNumber)
            statetuple = (0,)  # 最近一次读取的状态码，还未实现
            timetuple = localtime(time())  # 获得年月日时分秒的元组
            temp = statetuple + timetuple[0:6] + (0, 0, 0) + readResult  # 补0的个数取决于状态寄存器长度
            q.put((masterslaveindex, temp))  # 不论真实从站的值是否改变，都将读取到的值写入队列
            sleep(0.01)


def writeSingleHolding(request):  # 写入单个Holding寄存器
    valueaddress = int.from_bytes(request[8:10], byteorder='big')  # 获取起始地址
    slaveID = floor(valueaddress / lenperslave)  # 真实从站序号
    valuestartaddress = valueaddress % lenperslave - lenslavestate + 1  # 真实从站寄存器起始地址
    value = int.from_bytes(request[10:12], byteorder='big')  # 要写入的值
    valuepoll[slaveID] = [valuestartaddress, value]  # 将写入地址、值传递给共享字典
    print(valuepoll)


def writeMultiHoldings(request):  # 写入多个Holding寄存器
    multivalueaddress = int.from_bytes(request[8:10], byteorder='big')  # 获取起始地址
    valuelen = int.from_bytes(request[10:12], byteorder='big')  # 获取数据长度
    valuelist = []
    for valueindex in range(13, len(request), 2):  # 获取写入值
        valuelist.append(int.from_bytes(request[valueindex:valueindex + 2], byteorder='big'))
    slaveID = floor(valuelen / lenperslave)  # 真实从站序号
    valuestartaddress = multivalueaddress % lenperslave - lenslavestate + 1  # 真实从站寄存器起始地址
    for multivalue in valuelist:
        valuepoll[slaveID] = [valuestartaddress, multivalue]  # 将写入地址、值传递给共享字典
        valuestartaddress += 1
        if valuestartaddress > lenperslave:
            valuestartaddress = 11
        sleep(0.1)


class SlaveProxy(modbus_tcp.TcpServer):
    def _handle(self, request):
        """handle a received sentence"""

        # gets a query for analyzing the request
        query = self._make_query()

        retval = call_hooks("modbus.Server.before_handle_request", (self, request))
        if retval:
            request = retval

        response = self._databank.handle_request(query, request)

        retval = call_hooks("modbus.Server.after_handle_request", (self, response))
        if retval:
            response = retval
        # 当功能码大于4即真实主站写入内容时，目前仅处理了写入holding
        command = request[7]
        if command == 6:
            writeSingleHolding(request)
        if command == 16:
            writeMultiHoldings(request)

        return response


def call_hooks(name, args):
    """call the function associated with the hook and pass the given args"""
    with _LOCK:
        try:
            for fct in _HOOKS[name]:
                retval = fct(args)
                if retval is not None:
                    return retval
        except KeyError:
            pass
        return None


def main():
    q = queue.Queue()
    server = SlaveProxy()
    server.start()
    slave_1 = server.add_slave(1)
    # 如果有Coil，则创建相应Coil区
    if CoilsNumber > 0:
        slave_1.add_block('1', cst.COILS, 0, slaveNumber * CoilsNumber)
    # 不论是否有Holding，都创建Holding区，并为每个从站预留lenslavestate个状态寄存器
    slave_1.add_block('4', cst.HOLDING_REGISTERS, 0, slaveNumber * (HoldingsNumber + lenslavestate))

    masterThread = []
    masterlist = []
    # 根据配置信息，创建代理主站子线程
    for threadslaveindex in range(len(indexspace)):
        masterlist.append(modbus_tcp.TcpMaster(host="114.55.136.36", port=1004, timeout_in_sec=5.0))
        masterThread.append(
            threading.Thread(target=modbusmaster, args=(q, masterlist[threadslaveindex], indexspace[threadslaveindex])))
        masterThread[threadslaveindex].start()
    while True:
        if q.empty():
            sleep(0.1)
            continue
        else:  # 如果队列不空，将相应的值写入代理从站
            slaveindex, refreshvalue = q.get()
            slave_1.set_values('4', slaveindex * (HoldingsNumber + lenslavestate), refreshvalue)


if __name__ == "__main__":
    main()
