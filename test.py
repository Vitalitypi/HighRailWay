import numpy as np
from datetime import datetime

file_path = './dataset/Highway01/data.txt'

with open(file_path, 'r') as file:
    content = file.read()

rows = []
infos = content.split('\n')
for row in infos:
    if row=='':
        continue
    info = row.split('\t')
    rows.append(info)
data = []
for i in range(0,len(rows),6):
    row = rows[i]
    arr = []
    date_format = "%d/%m/%Y"

    t = datetime.strptime(row[0], date_format)
    for j in range(0,6,2):
        info = []
        if rows[i+j][3]=='':
            info.append(0)
        else:
            info.append(int(rows[i+j][3]))
        try:
            if rows[i+j+1][3]=='':
                info.append(0)
            else:
                info.append(int(rows[i+j+1][3]))
        except:
            print('err')
        info.append(int(rows[i+j][2]))
        info.append(int(rows[i+j+1][2]))
        info.append(t.weekday())
        arr.append(info)
    data.append(arr)

data = np.array(data)
print(data.shape)
np.savez_compressed('highway.npz', arr=data)
