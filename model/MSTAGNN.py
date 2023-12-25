import argparse
import configparser

import torch
from torch import nn
from torchinfo import summary
import torch.nn.functional as F

class AttentionLayer(nn.Module):
    """Perform attention across the -2 dim (the -1 dim is `model_dim`).

    Make sure the tensor is permuted to correct shape before attention.

    E.g.
    - Input shape (batch_size, in_steps, num_nodes, model_dim).
    - Then the attention will be performed across the nodes.

    Also, it supports different src and tgt length.

    But must `src length == K length == V length`.

    """

    def __init__(self, model_dim, num_heads=8, mask=False):
        super().__init__()

        self.model_dim = model_dim
        self.num_heads = num_heads
        self.mask = mask

        self.head_dim = model_dim // num_heads

        # self.FC_Q = nn.Linear(model_dim, model_dim)
        # self.FC_K = nn.Linear(model_dim, model_dim)
        # self.FC_V = nn.Linear(model_dim, model_dim)

        # self.out_proj = nn.Linear(model_dim, model_dim)

    def forward(self, query, key, value):
        # Q    (batch_size, ..., tgt_length, model_dim)
        # K, V (batch_size, ..., src_length, model_dim)
        batch_size = query.shape[0]
        tgt_length = query.shape[-2]
        src_length = key.shape[-2]

        # query = self.FC_Q(query)
        # key = self.FC_K(key)
        # value = self.FC_V(value)

        # Qhead, Khead, Vhead (num_heads * batch_size, ..., length, head_dim)
        query = torch.cat(torch.split(query, self.head_dim, dim=-1), dim=0)
        key = torch.cat(torch.split(key, self.head_dim, dim=-1), dim=0)
        value = torch.cat(torch.split(value, self.head_dim, dim=-1), dim=0)

        key = key.transpose(
            -1, -2
        )  # (num_heads * batch_size, ..., head_dim, src_length)

        attn_score = (
            query @ key
        ) / self.head_dim**0.5  # (num_heads * batch_size, ..., tgt_length, src_length)

        if self.mask:
            mask = torch.ones(
                tgt_length, src_length, dtype=torch.bool, device=query.device
            ).tril()  # lower triangular part of the matrix
            attn_score.masked_fill_(~mask, -torch.inf)  # fill in-place

        attn_score = torch.softmax(attn_score, dim=-1)
        out = attn_score @ value  # (num_heads * batch_size, ..., tgt_length, head_dim)
        out = torch.cat(
            torch.split(out, batch_size, dim=0), dim=-1
        )  # (batch_size, ..., tgt_length, head_dim * num_heads = model_dim)

        # out = self.out_proj(out)

        return out

class SelfAttentionLayer(nn.Module):
    def __init__(
        self, model_dim, feed_forward_dim=2048, num_heads=8, dropout=0, mask=False
    ):
        super().__init__()

        self.attn = AttentionLayer(model_dim, num_heads, mask)
        self.feed_forward = nn.Sequential(
            nn.Linear(model_dim, feed_forward_dim),
            nn.ReLU(inplace=True),
            nn.Linear(feed_forward_dim, model_dim),
        )
        self.ln1 = nn.LayerNorm(model_dim)
        self.ln2 = nn.LayerNorm(model_dim)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

    def forward(self, x, dim=-2):
        x = x.transpose(dim, -2)
        # x: (batch_size, ..., length, model_dim)
        residual = x
        out = self.attn(x, x, x)  # (batch_size, ..., length, model_dim)
        out = self.dropout1(out)
        out = self.ln1(residual + out)

        residual = out
        out = self.feed_forward(out)  # (batch_size, ..., length, model_dim)
        out = self.dropout2(out)
        out = self.ln2(residual + out)

        out = out.transpose(dim, -2)
        return out

class Encoder(nn.Module):
    def __init__(
            self,
            num_nodes,
            batch_size,
            periods_embedding_dim,
            weekend_embedding_dim,
            input_dim,                      # flow, day, weekend, holiday
            periods=288,
            weekend=7,
            embed_dim=12,
            in_steps=12,
    ):
        super(Encoder, self).__init__()

        self.num_nodes = num_nodes
        self.periods = periods
        self.weekend = weekend
        # 输入的embedding维度
        # 周期的embedding维度
        self.periods_embedding_dim = periods_embedding_dim
        # 每周的embedding维度
        self.weekend_embedding_dim = weekend_embedding_dim
        self.in_steps = in_steps
        self.input_dim = input_dim
        # period的embedding
        if periods_embedding_dim>0:
            self.periods_embedding = nn.Embedding(periods, periods_embedding_dim)
        # 每周的embedding
        if weekend_embedding_dim>0:
            self.weekend_embedding = nn.Embedding(weekend, weekend_embedding_dim)
    def forward(self, x):
        '''
        获取当前的动态图
        :param x:
        shape:b,ti,n,di
        :return:
        shape:b,to,n,do
        '''

        features = []

        if self.periods_embedding_dim > 0:
            periods = x[..., 1]
            periods_emb = self.periods_embedding(
                (periods*self.periods).long()
            )
            features.append(periods_emb)
            # time_embedding = torch.mul(time_embedding, periods_emb[:,:,0])


        if self.weekend_embedding_dim > 0:
            weekend = x[..., 2]
            weekend_emb = self.weekend_embedding(
                weekend.long()
            )  # (batch_size, in_steps, num_nodes, weekend_embedding_dim)
            features.append(weekend_emb)
            # time_embedding = torch.mul(time_embedding, weekend_emb[:,:,0])
        encoding = torch.cat(features,dim=-1)          # 4 * dim_embed + dim_input_emb
        return encoding

class MSTAGNN(nn.Module):
    def __init__(
            self,
            num_nodes,              #节点数
            batch_size,
            input_dim,              #输入维度
            rnn_units,              #GRU循环单元数
            output_dim,             #输出维度
            num_layers,               #GRU的层数
            embed_dim,              #GNN嵌入维度
            in_steps=12,            #输入的时间长度
            out_steps=12,           #预测的时间长度
            last_steps=1,
            st_steps=1,
            periods=288,
            weekend=7,
            periods_embedding_dim=6,
            weekend_embedding_dim=6,
            num_input_dim=1,
    ):
        super(MSTAGNN, self).__init__()
        assert num_input_dim <= input_dim
        self.num_node = num_nodes
        self.input_dim = input_dim
        self.num_input_dim = num_input_dim
        self.hidden_dim = rnn_units
        self.output_dim = output_dim
        self.in_steps = in_steps
        self.out_steps = out_steps
        self.embed_dim = embed_dim
        self.num_layers = num_layers
        self.periods_embedding_dim = periods_embedding_dim
        self.weekend_embedding_dim = weekend_embedding_dim
        self.encoder = Encoder(num_nodes, batch_size, periods_embedding_dim, weekend_embedding_dim, input_dim, periods,
                               weekend, embed_dim, in_steps)
        self.node_embeddings = nn.Parameter(torch.randn(num_nodes, embed_dim), requires_grad=True)
        self.time_embeddings = nn.Parameter(torch.randn(batch_size,in_steps, embed_dim), requires_grad=True)

        self.predictor = MSTARNN(num_nodes, num_input_dim, rnn_units, embed_dim, num_layers, in_steps, out_steps, dim_out=output_dim,
                        st_steps=st_steps,conv_steps=last_steps)
        self.last_steps = last_steps

    def forward(self, source):
        batch_size = source.shape[0]
        encoding = self.encoder(source)
        node_embedding = self.node_embeddings
        time_embedding = self.time_embeddings[:batch_size]
        if self.periods_embedding_dim > 0:
            emb_periods = encoding[...,:self.embed_dim]
            time_embedding = torch.mul(time_embedding, emb_periods[:,:,0])
        if self.weekend_embedding_dim>0:
            emb_weekend = encoding[...,self.embed_dim:]
            time_embedding = torch.mul(time_embedding, emb_weekend[:,:,0])
        init_state = self.predictor.init_hidden(batch_size)#,self.num_node,self.hidden_dim
        _, output = self.predictor(source[...,:self.num_input_dim], init_state, [node_embedding, time_embedding]) # B, T, N, hidden
        return output

class MSTARNN(nn.Module):
    def __init__(self, num_nodes, dim_in, dim_hidden, dim_embed, num_layers, in_steps=12, out_steps=12, dim_out=1,
            st_steps=1, conv_steps=2, conv_bias=True):
        super(MSTARNN, self).__init__()
        assert num_layers >= 1, 'At least one GRU layer in the RNN.'
        self.num_nodes = num_nodes
        self.input_dim = dim_in
        self.dim_hidden = dim_hidden
        self.st_steps = st_steps
        self.num_layers = num_layers
        self.out_steps = out_steps
        self.grus = nn.ModuleList([
            MSTACell(num_nodes, dim_in, dim_hidden, dim_embed, st_steps)
            for _ in range(self.num_layers)
        ])
        # predict output
        self.predictors = nn.ModuleList([
             # nn.Linear(hidden_dim,dim_cat)
             nn.Conv2d(conv_steps, dim_out * out_steps, kernel_size=(1,dim_hidden), bias=conv_bias)
             for _ in range(self.num_layers)
        ])
        # skip
        self.skips = nn.ModuleList([
            # nn.Linear(dim_hidden+1,dim_in)
            nn.Conv2d(conv_steps, dim_in * in_steps, kernel_size=(1,dim_hidden), bias=conv_bias)
            for _ in range(self.num_layers-1)
        ])
        # dropout
        self.dropouts = nn.ModuleList([
            nn.Dropout(p=0.1)
            for _ in range(self.num_layers)
        ])
        self.conv_steps = conv_steps
    def forward(self, x, init_state, embeddings):
        # shape of x: (B, T, N, D)
        assert x.shape[2] == self.num_nodes and x.shape[3] == self.input_dim

        outputs = []
        batch_size = x.shape[0]
        seq_length = x.shape[1] # T
        current_inputs = x
        skip = current_inputs
        state = init_state.to(x.device)
        for i in range(self.num_layers):
            inner_states = [state]
            for t in range(0,seq_length,self.st_steps):
                inp_h = torch.cat(inner_states, dim=1)
                inp_x = current_inputs[:, t:t+self.st_steps, :, :]
                state = self.grus[i](inp_x, inp_h, [embeddings[0], embeddings[1][:, t:t+self.st_steps, :]])
                inner_states.append(state)
            current_inputs = torch.cat(inner_states[1:], dim=1) # [B, T, N, D]
            current_inputs = self.dropouts[i](current_inputs[:, -self.conv_steps:, :, :])
            outputs.append(self.predictors[i](current_inputs).reshape(batch_size,self.out_steps,self.num_nodes,-1))
            if i < self.num_layers-1:
                current_inputs = skip - self.skips[i](current_inputs)

        # predict = torch.cat(outputs,dim=-1)
        predict = outputs[0]
        for i in range(1,len(outputs)):
            predict = predict + outputs[i]
        return None, predict

    def init_hidden(self, batch_size):
        return self.grus[0].init_hidden_state(batch_size)

class MSTACell(nn.Module):
    def __init__(self,num_nodes,dim_in,dim_out,dim_embed,st_steps=3):
        super(MSTACell, self).__init__()
        self.dim_hidden = dim_out
        self.num_nodes = num_nodes
        self.st_steps = st_steps
        self.gate_z = TAGCM(dim_in+self.dim_hidden, dim_out, dim_embed, num_nodes, st_steps)
        self.gate_r = TAGCM(dim_in+self.dim_hidden, dim_out, dim_embed, num_nodes, st_steps)
        self.update = TAGCM(dim_in+self.dim_hidden, dim_out, dim_embed, num_nodes, st_steps)
    def forward(self,x, states, embeddings):
        '''
        :param x:
            b,steps,n,di
        :param state:
            b,steps,n,dh
        :param embedding:
             [(n,d),(b,steps,d)]
        :return:
            b,steps,n,dh
        '''
        state = states[:,-self.st_steps:]
        states = states.permute(0, 2, 1, 3)
        input_and_state = torch.cat((x, state), dim=-1) # [B, N, 1+D]
        z = torch.sigmoid(self.gate_z(input_and_state, states, embeddings))
        r = torch.sigmoid(self.gate_r(input_and_state, states, embeddings))
        candidate = torch.cat((x, z*state), dim=-1)
        hc = torch.tanh(self.update(candidate, states, embeddings))
        h = r*state + (1-r)*hc
        return h

    def init_hidden_state(self, batch_size):
        return torch.zeros(batch_size, self.st_steps, self.num_nodes, self.dim_hidden)

class TAGCM(nn.Module):
    def __init__(self,dim_in,dim_out,dim_embed,num_nodes,st_steps=3,num_heads=4,mask=False,dropout=0.1,kernel_size=0):
        super(TAGCM, self).__init__()
        self.st_steps = st_steps
        self.kernel_size = kernel_size
        if kernel_size > 0:
            self.padding = (kernel_size - 1)//2
            self.tcn = nn.Conv2d(dim_out, dim_out, (1, kernel_size), padding=(0, self.padding))
        self.gcn = DSTGCN(dim_in, dim_out, dim_embed, num_nodes,st_steps)
        self.attn = AttentionLayer(dim_out, num_heads, mask)
        self.dropout = nn.Dropout(dropout)
        self.norm = nn.LayerNorm(dim_out)
    def forward(self, x, states, embeddings):
        '''
        :param x:
            b,steps,n,di
        :param states:
            b,n,t,do
        :param embeddings:
            [(n,de),(b,steps,de)]
        :return:
            b,n,d
        '''
        if self.kernel_size > 0:
            states = states.permute(0,3,2,1)
            states = self.tcn(states).permute(0,3,2,1)
        # 首先通过1个GCN将x维度升至dim_hidden
        x = self.gcn(x,embeddings).permute(0,2,1,3)     # b,n,steps,do
        residual = x
        state = self.attn(self.norm(x),states,states)
        state = residual + self.dropout(state)          # b,n,steps,do
        return state.permute(0,2,1,3)

class DSTGCN(nn.Module):
    def __init__(self,dim_in,dim_out,dim_embed,num_nodes,st_steps=3):
        super(DSTGCN, self).__init__()
        self.num_nodes = num_nodes
        self.st_steps = st_steps
        self.dim_embed = dim_embed
        self.dim_out = dim_out
        self.weights_pool = nn.Parameter(torch.FloatTensor(dim_embed, 2, dim_in, dim_out)) # [D, C, F]
        self.bias_pool = nn.Parameter(torch.FloatTensor(dim_embed, dim_out)) # [D, F]
        self.norm = nn.LayerNorm(dim_embed, eps=1e-12)
        self.drop = nn.Dropout(0.1)

    def forward(self,x,embeddings):
        '''
        :param x:
            b,3,n,di
        :param embeddings:
            [(n,d),(b,3,d)]
        :return:
            b,3,n,do
        '''
        b,t,n,d = x.size()
        node_embeddings, time_embeddings = embeddings[0],embeddings[1]
        node_embeddings = torch.stack([node_embeddings for _ in range(t)],dim=0)            # 3,n,d
        supports1 = torch.eye(self.num_nodes).to(x.device)                                # n,n
        supports1 = torch.stack([supports1 for _ in range(t)],dim=0)
        embedding = self.drop(
            self.norm(node_embeddings.unsqueeze(0) + time_embeddings.unsqueeze(-2)))        # b,3,n,d
        # embedding = embedding.view(b,t*n,self.dim_embed)
        supports2 = F.softmax(torch.matmul(embedding, embedding.transpose(-2, -1)), dim=-1)   # b,3,n,n

        # node_embeddings = node_embeddings.view(t*n,self.dim_embed)


        x_g1 = torch.einsum("tnm,btmc->btnc", supports1, x)
        x_g2 = torch.einsum("btnm,btmc->btnc", supports2, x)

        x_g = torch.stack([x_g1,x_g2],dim=1)        # b,2,t,n,d
        weights = torch.einsum('tnd,dkio->tnkio', node_embeddings, self.weights_pool) # N, dim_in, dim_out
        bias = torch.einsum('btd,do->bto', time_embeddings, self.bias_pool) # b, t, o
        x_g = x_g.permute(0, 2, 3, 1, 4)  # B, t, n, k, i

        st_gconv = torch.einsum('btnki,tnkio->btno', x_g, weights) + bias.unsqueeze(-2)  # b, t, 1, o
        return st_gconv

class Network(nn.Module):
    def __init__(self, args):
        super(Network, self).__init__()
        self.mgstgnn = MSTAGNN(args.num_nodes,args.batch_size,args.input_dim,args.rnn_units,args.output_dim,args.num_layers,args.embed_dim,
                in_steps=args.in_steps,out_steps=args.out_steps,last_steps=args.last_steps,st_steps=args.st_steps,
                periods=args.periods,weekend=args.weekend,periods_embedding_dim=args.periods_embedding_dim,
                weekend_embedding_dim=args.weekend_embedding_dim,num_input_dim=args.num_input_dim)
    def forward(self,x):

        out = self.mgstgnn(x)
        return out

if __name__ == "__main__":
    args = argparse.ArgumentParser(description='arguments')
    args.add_argument('--dataset', default='PEMS08', type=str)
    args.add_argument('--mode', default='train', type=str)
    args.add_argument('--device', default='cuda:0', type=str, help='indices of GPUs')
    args.add_argument('--debug', default='False', type=eval)
    args.add_argument('--model', default='MGSTGNN', type=str)
    args.add_argument('--cuda', default=True, type=bool)
    args1 = args.parse_args()

    #get configuration
    config_file = '../config/{}.conf'.format(args1.dataset)
    #print('Read configuration file: %s' % (config_file))
    config = configparser.ConfigParser()
    config.read(config_file)

    #data
    args.add_argument('--val_ratio', default=config['data']['val_ratio'], type=float)
    args.add_argument('--test_ratio', default=config['data']['test_ratio'], type=float)
    args.add_argument('--in_steps', default=config['data']['in_steps'], type=int)
    args.add_argument('--out_steps', default=config['data']['out_steps'], type=int)
    args.add_argument('--num_nodes', default=config['data']['num_nodes'], type=int)
    args.add_argument('--normalizer', default=config['data']['normalizer'], type=str)
    args.add_argument('--adj_norm', default=config['data']['adj_norm'], type=eval)
    #model
    args.add_argument('--input_dim', default=config['model']['input_dim'], type=int)

    args.add_argument('--num_input_dim', default=config['model']['num_input_dim'], type=int)

    args.add_argument('--periods_embedding_dim', default=config['model']['periods_embedding_dim'], type=int)
    args.add_argument('--weekend_embedding_dim', default=config['model']['weekend_embedding_dim'], type=int)

    args.add_argument('--output_dim', default=config['model']['output_dim'], type=int)
    args.add_argument('--embed_dim', default=config['model']['embed_dim'], type=int)
    args.add_argument('--rnn_units', default=config['model']['rnn_units'], type=int)
    args.add_argument('--num_layers', default=config['model']['num_layers'], type=int)
    args.add_argument('--periods', default=config['model']['periods'], type=int)
    args.add_argument('--weekend', default=config['model']['weekend'], type=int)
    args.add_argument('--last_steps', default=config['model']['last_steps'], type=int)
    args.add_argument('--st_steps', default=config['model']['st_steps'], type=int)
    #train
    args.add_argument('--loss_func', default=config['train']['loss_func'], type=str)
    args.add_argument('--random', default=config['train']['random'], type=eval)
    args.add_argument('--seed', default=config['train']['seed'], type=int)
    args.add_argument('--batch_size', default=config['train']['batch_size'], type=int)
    args.add_argument('--epochs', default=config['train']['epochs'], type=int)
    args.add_argument('--lr_init', default=config['train']['lr_init'], type=float)
    args.add_argument('--lr_decay', default=config['train']['lr_decay'], type=eval)
    args.add_argument('--lr_decay_rate', default=config['train']['lr_decay_rate'], type=float)
    args.add_argument('--lr_decay_step', default=config['train']['lr_decay_step'], type=str)
    args.add_argument('--early_stop', default=config['train']['early_stop'], type=eval)
    args.add_argument('--early_stop_patience', default=config['train']['early_stop_patience'], type=int)
    args.add_argument('--grad_norm', default=config['train']['grad_norm'], type=eval)
    args.add_argument('--max_grad_norm', default=config['train']['max_grad_norm'], type=int)
    args.add_argument('--real_value', default=config['train']['real_value'], type=eval, help = 'use real value for loss calculation')

    #test
    args.add_argument('--mae_thresh', default=config['test']['mae_thresh'], type=eval)
    args.add_argument('--mape_thresh', default=config['test']['mape_thresh'], type=float)
    #log
    args.add_argument('--log_dir', default='./', type=str)
    args.add_argument('--log_step', default=config['log']['log_step'], type=int)
    args.add_argument('--plot', default=config['log']['plot'], type=eval)
    args = args.parse_args()
    from utils.util import init_seed
    init_seed(args.seed)
    model = Network(args)
    summary(model, [args.batch_size, args.in_steps, args.num_nodes, args.input_dim])
