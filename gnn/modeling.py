import torch
from torch import nn
from torch.nn.utils.rnn import pad_sequence
import torch.nn.functional as F
from torch_geometric.data import NeighborSampler
import torch_geometric.nn as pyg_nn
from tqdm.notebook import tqdm
import numpy as np


class GNN(nn.Module):
    def __init__(self, input_size, hidden_size, output_size, num_layers=2, model="GraphSage"):
        super().__init__()
        self.num_layers = num_layers

        conv_model = self._build_conv_model(model)
        self.convs = nn.ModuleList()
        self.convs.append(conv_model(input_size, hidden_size))
        for _ in range(self.num_layers - 1):
            self.convs.append(conv_model(hidden_size, hidden_size))

        self.convs.append(conv_model(hidden_size, output_size))

        
    def forward(self, x, adjs):
        # `train_loader` computes the k-hop neighborhood of a batch of nodes,
        # and returns, for each layer, a bipartite graph object, holding the
        # bipartite edges `edge_index`, the index `e_id` of the original edges,
        # and the size/shape `size` of the bipartite graph.
        # Target nodes are also included in the source nodes so that one can
        # easily apply skip-connections or add self-loops.
        for i, (edge_index, _, size) in enumerate(adjs):
            x_target = x[:size[1]]  # Target nodes are always placed first.
            x = self.convs[i]((x, x_target), edge_index)
            if i != self.num_layers - 1:
                x = F.relu(x)
                x = F.dropout(x, p=0.5, training=self.training)
        return x

    def _build_conv_model(self, model_type="GraphSage"):
        if model_type == 'GCN':
            return pyg_nn.GCNConv
        if model_type == 'GAT':
            return pyg_nn.GATConv
        if model_type == "GraphSage":
            return pyg_nn.SAGEConv
        if model_type == "TransformerConv":
            return pyg_nn.TransformerConv

        raise ValueError(
            f"Model {model_type} unavailable, please add it to GNN.build_conv_model.")


class HybridNetwork(nn.Module):
    def __init__(self, res_size, user_size, hidden_size, output_size, num_layers=2, model="GraphSage", k=5):
        super().__init__()

        self.k = k  # number of users for each business
        self.res_GNN = GNN(res_size, hidden_size, hidden_size, num_layers=2, model="GraphSage")
        self.user_GNN = GNN(user_size, hidden_size, hidden_size, num_layers=2, model="GraphSage")
        self.out_layer = nn.Linear(hidden_size * (self.k + 1), output_size)

    def forward(self, res_x, res_adjs, user_x, user_adj, inverse_idx, num_users_per_res):
        res_x = self.res_GNN(res_x, res_adjs)  # [batch size, hidden size]
        user_x = self.user_GNN(user_x, user_adj)  # [all_unique_users, hidden size]
        user_x = user_x[inverse_idx]  # [all users per network, hidden size]
        user_x = torch.split(user_x, num_users_per_res)  # [batch size, # of user per restaruant, hidden size]
        user_x = pad_sequence(user_x, batch_first=True, padding_value=0.0)  # [batch size, k, hidden size]
        user_x = torch.reshape(user_x, (user_x.shape[0], -1))  # [batch size, k * hidden size]

        emb = torch.cat([res_x, user_x], 1)  # [batch size, hidden size * (k + 1)]
        out = self.out_layer(emb)
        return out


def train(model, num_epochs, dataset, train_loader, optimizer, criterion, device="cpu", k=5):
    for epoch in range(num_epochs):
        model.train()

        pbar = tqdm(total=int(dataset.train_index.shape[0]))
        pbar.set_description(f'Epoch {(epoch + 1):02d}')

        total_loss = total_correct = 0
        steps = 0
        num_examples = 0
        for batch_size, n_id, adjs in train_loader:
            # `adjs` holds a list of `(edge_index, e_id, size)` tuples.
            adjs = [adj.to(device) for adj in adjs]

            num_users_per_res, users = dataset.get_visited_users(n_id[:batch_size], k)
            unique_users, inverse_idx = np.unique(users, return_inverse=True)
            unique_users = torch.LongTensor(unique_users)

            user_loader = NeighborSampler(dataset.user_pyg_graph.edge_index, node_idx=unique_users,
                                          sizes=[10, 10], batch_size=unique_users.shape[0], shuffle=False)
            
            for num_users, u_id, u_adjs in user_loader:
                # this is actually just one loop: using for loop since 
                # next(iter(user_loader)) appears to be buggy
                u_adjs = [adj.to(device) for adj in u_adjs]

            del user_loader

            optimizer.zero_grad()
            out = model(dataset.res_x[n_id], adjs, dataset.user_x[u_id], u_adjs, inverse_idx, num_users_per_res)
            loss = criterion(out, dataset.labels[n_id[:batch_size]])
            loss.backward()
            optimizer.step()

            total_loss += loss.item()

            pred = out.argmax(dim=-1)
            total_correct += int(pred.eq(dataset.labels[n_id[:batch_size]]).sum())
            steps += 1
            num_examples += batch_size
            
            pbar.set_postfix(accuracy=total_correct / num_examples, curr_loss=total_loss / steps)
            pbar.update(batch_size)

        pbar.close()

        loss = total_loss / len(train_loader)
        approx_acc = total_correct / int(dataset.train_index.shape[0])

        print(f'Epoch {epoch + 1:02d}, Loss: {loss:.4f}, Approx. Train: {approx_acc:.4f}')

#         train_acc, val_acc, test_acc = test()
#         print(f'Train: {train_acc:.4f}, Val: {val_acc:.4f}, '
#               f'Test: {test_acc:.4f}')


# @torch.no_grad()
# def test(model, dataset):
#     model.eval()

#     out = model.inference(x)

#     y_true = dataset.labels.cpu().unsqueeze(-1)
#     y_pred = out.argmax(dim=-1, keepdim=True)

#     results = []
#     for mask in [dataset.train_mask, dataset.val_mask, dataset.test_mask]:
#         results += [int(y_pred[mask].eq(y_true[mask]).sum()) / int(mask.sum())]

#     return results