#!/bin/bash

random=("1" "2" "3")

datasets=('PEMS03')

last_steps=("1" "3" "4" "5")

dim_hidden=("16" "32" "48" "64" "80")

dim_embed_4=("4" "6" "8" "10" "12")
dim_embed_8=("8" "10" "12" "14" "16")

# 首先进行测试超参数 num_k
for k in "${last_steps[@]}"
do
    echo "当前参数：last_steps:$k"
    for dataset in "${datasets[@]}"
    do
        echo "当前数据集：$dataset"
        # 更新conf文件
        sed -i "s/^last_steps=.*/last_steps= $k/" ./config/${dataset}.conf
        sed -i "s/^random=.*/random= True/" ./config/${dataset}.conf
        for rand in "${random[@]}"
        do
            echo "当前随机次数：$rand"
            python main.py --dataset $dataset >> ./exps/random/${dataset}-p12w0-rnn64-lay2-last_${k}-st1-random_${rand}.log 2>&1
        done
    done
done
