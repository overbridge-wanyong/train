import pandas as pd
import torch
from torch.utils.data import Dataset, DataLoader
from scipy import stats

import math
import torch
import torch.nn as nn

torch.manual_seed(37)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)

class PermutedGruCell(nn.Module):
    def __init__(self, hidden_size, bias):
        super().__init__()
        self.hidden_size = hidden_size
        self.W_ir = nn.Parameter(torch.empty(hidden_size, hidden_size))
        self.W_hr = nn.Parameter(torch.empty(hidden_size, hidden_size))
        self.W_iz = nn.Parameter(torch.empty(hidden_size, hidden_size))
        self.W_hz = nn.Parameter(torch.empty(hidden_size, hidden_size))
        self.W_in = nn.Parameter(torch.empty(hidden_size, hidden_size))
        self.W_hn = nn.Parameter(torch.empty(hidden_size, hidden_size))
        self.reset_parameters()

    def reset_parameters(self):
        for w in self.parameters():
            nn.init.kaiming_uniform_(w, a=math.sqrt(5))

    def forward(self, x, lower, hidden=None):
        if hidden is None:
            hidden = torch.zeros(x.size(0), self.hidden_size).to(device)
        W_ir = self.W_ir * lower.to(device)
        W_hr = self.W_hr * lower.to(device)
        W_iz = self.W_iz * lower.to(device)
        W_hz = self.W_hz * lower.to(device)
        W_in = self.W_in * lower.to(device)
        W_hn = self.W_hn * lower.to(device)
        sigmoid = nn.Sigmoid()
        tanh = nn.Tanh()
        r_t = sigmoid(torch.matmul(x, W_ir) + torch.matmul(hidden, W_hr)).to(device)
        z_t = sigmoid(torch.matmul(x, W_iz) + torch.matmul(hidden, W_hz)).to(device)
        n_t = tanh(torch.matmul(x, W_in) + torch.matmul(r_t * hidden, W_hn)).to(device)
        hy = (hidden * z_t + (1.0 - z_t) * n_t).to(device)
        return hy


class PermutationMatrix(nn.Module):
    def __init__(self, input_size, temperature=1000, unroll=20):
        super().__init__()
        self.unroll, self.temperature = unroll, temperature
        self.matrix = nn.Parameter(torch.empty(input_size, input_size))
        nn.init.kaiming_uniform_(self.matrix, a=math.sqrt(5))
        self.lower = torch.tril(torch.ones(input_size, input_size)).to(device)

    def forward(self, verbose=False):
        print("permutation matrix has nan: ", torch.sum(torch.isnan(self.matrix)))
        matrix_shape = self.matrix.shape[0]
        max_row = torch.max(self.matrix, dim=1).values.reshape(matrix_shape, 1).to(device)
        ones = torch.ones(matrix_shape).reshape(1, matrix_shape).to(device)
        matrix = torch.exp(self.temperature * (self.matrix - torch.matmul(max_row, ones))).to(device)
        for _ in range(self.unroll):
            matrix = matrix / torch.sum(matrix, dim=1, keepdim=True)
            matrix = matrix / torch.sum(matrix, dim=0, keepdim=True)
        output_lower = torch.matmul(torch.matmul(matrix, self.lower), matrix.t()).t().to(device)
        ideal_matrix_order = matrix.data.argmax(dim=1, keepdim=True)
        new_matrix = torch.zeros_like(matrix)
        new_matrix.scatter_(
            1, ideal_matrix_order, torch.ones_like(ideal_matrix_order).float()
        )
        causal_order = [(idx, int(d[0])) for idx, d in enumerate(ideal_matrix_order)]
        causal_order.sort(key=lambda x: x[1])
        causal_order = [d[0] for d in causal_order]
        if verbose:
            row_sum = round(float(torch.median(torch.sum(matrix, dim=1)[0])), 2)
            col_sum = round(float(torch.median(torch.sum(matrix, dim=0)[0])), 2)
            row_max = round(float(torch.median(torch.max(matrix, dim=1)[0])), 2)
            col_max = round(float(torch.median(torch.max(matrix, dim=0)[0])), 2)
            print(
                "Median Row Sum: {}, Col Sum: {} Row Max: {} Col Max: {}".format(
                    row_sum, col_sum, row_max, col_max
                )
            )
            print("Permutation Matrix\n", matrix.data.numpy().round(1))
            print(
                "Permuted Lower Triangular Matrix\n",
                output_lower.t().data.numpy().round(1),
            )
            print("Ideal Permutation Matrix\n", new_matrix.data)
            print(
                "Ideal Lower Triangular\
                    matrix\n",
                torch.matmul(torch.matmul(new_matrix, self.lower), new_matrix.t()),
            )
            print("Causal Order\n", causal_order)

        return output_lower


class PermutedGru(nn.Module):
    def __init__(
        self,
        hidden_size,
        bias=False,
        num_layers=1,
        batch_first=False,
        dropout=0.0,
    ):
        super().__init__()
        self.cell = PermutedGruCell(hidden_size=hidden_size, bias=False)
        self.batch_first = batch_first
        self.permuted_matrix = PermutationMatrix(hidden_size)

    def forward(self, input_, lengths=None, hidden=None):
        # input_ is of dimensionalty (T, B, hidden_size, ...)
        # lenghths is B,
        dim = 1 if self.batch_first else 0
        lower = self.permuted_matrix(verbose=False) # Verbose Flag
        outputs = []
        for x in torch.unbind(input_, dim=dim):  # x dim is B, I
            hidden = self.cell(x, lower, hidden)
            outputs.append(hidden.clone())

        hidden_states = torch.stack(outputs)  # T, B, H
        last_states = []
        if lengths is None:
            lengths = [len(input_)] * len(input_[0])
        for idx, l in enumerate(lengths):
            last_states.append(hidden_states[l - 1, idx, :])
        last_states = torch.stack(last_states)
        return hidden_states, last_states


class PermutedDKT(nn.Module):
    def __init__(self, n_constructs):
        super().__init__()
        self.gru = PermutedGru(n_constructs, batch_first=False)
        self.n_constructs = n_constructs
        self.output_layer = nn.Linear(1, 1)

    def forward(self, construct_input, labels):
        # Input shape is T, B
        # Input[i,j]=k at time i, for student j, construct k is attended
        # label is T,B 0/1
        T, B = construct_input.shape
        input = torch.zeros(T, B, self.n_constructs).to(device)
        input.scatter_(2, construct_input.unsqueeze(2), labels.unsqueeze(2).float())
        hidden_states, _ = self.gru(input)
        init_state = torch.zeros(1, input.shape[1], input.shape[2]).to(device)
        shifted_hidden_states = torch.cat([init_state, hidden_states], dim=0)[1:, :, :]
        output = torch.sigmoid(self.output_layer(shifted_hidden_states.unsqueeze(3)).squeeze(3))
        output = torch.gather(output, 2, construct_input.unsqueeze(2)).squeeze(2)
        pred = (output >= 0.5).float()
        acc = torch.mean((pred == torch.clamp(labels, min=0).float()).float())
        cc_loss = nn.BCELoss()
        loss = cc_loss(output, torch.clamp(labels, min=0).float())
        return loss, acc

###########################
### Create TrainingDataset
###########################
class TrainingDataset(Dataset):
    def __init__(self, constructs, labels, tot_construct_set):
        self.constructs = constructs
        self.labels = labels
        # Put number of construct here?
        self.n_constructs = len(tot_construct_set)
        self.unique_construct_list = list(tot_construct_set)
    def __len__(self):
        return len(self.labels)
    def __getitem__(self, idx):
        construct = self.constructs[idx]
        label = self.labels[idx]
        sample = {"Construct": construct, "Label": label}
        return sample

def createDataset(filename: str):
    lessons_df = pd.read_csv(filename)
    checkin_df = lessons_df[lessons_df['Type'] == 'Checkin']
    simple_df = checkin_df.iloc[:, [2, 5, 9]] 
    simple_df.loc[simple_df["IsCorrect"] == 0, "IsCorrect"] = -1 

    num_of_questions = stats.mode(simple_df["UserId"]).count[0]

    tot_construct_set = set()
    tot_construct_list = list()
    tot_label_list = list()
    
    for user, user_info in simple_df.groupby('UserId'):

        constructs = user_info["ConstructId"].values.tolist() # [C]
        labels = user_info["IsCorrect"].values.tolist() # [C]

        tot_construct_set.update(constructs)

        num_of_constructs = len(constructs)
        pad_needed = num_of_questions - num_of_constructs # [P = Q - C]

        constructs += [0] * pad_needed # [Q]
        labels += [0] * pad_needed # [Q]

        tot_construct_list.append(constructs)
        tot_label_list.append(labels)

        # serialized_constructs = list(map(lambda construct: self.unique_construct_list.index(construct), constructs))
        # tot_serialized_construct_list.append(serialized_constructs)

    # Need to add 0 as for construct not learned by the student
    tot_construct_set.add(0)

    ######################
    ### How to store TD?
    ######################
    # Added tot_construct_set for initialization to store the number of total constructs.
    # 1) [Q, S] as in transposed format
    # TD = TrainingDataset(list(map(list, zip(*tot_construct_list))), list(map(list, zip(*tot_label_list))), tot_construct_set)
    # 2) [S, Q]
    # TD = TrainingDataset(tot_construct_list, tot_label_list, tot_construct_set)


    ########################
    ### For One-Hot encoding
    #########################
    tot_serialized_construct_list = list()
    unique_construct_list = list(tot_construct_set)
    # print(unique_construct_list)
    for constructs in tot_construct_list:
        # print("constructs: ", constructs)
        serialized_constructs = list(map(lambda x: unique_construct_list.index(x), constructs))
        tot_serialized_construct_list.append(serialized_constructs)
    TD = TrainingDataset(tot_serialized_construct_list, tot_label_list, tot_construct_set)

    # construct_label_df = pd.DataFrame({'Construct' : tot_construct_list, 'Label' : tot_label_list})
    # TD = TrainingDataset(construct_label_df['Construct'], construct_label_df['Label'], tot_construct_set)

    # # Display text and label.
    # print('\nFirst iteration of data set: ', next(iter(TD)), '\n')
    # # Print how many items are in the data set
    # print('Length of data set: ', len(TD), '\n')
    # # Print entire data set
    # print('Entire data set: ', list(DataLoader(TD)), '\n')

    # DL = DataLoader(TD, batch_size=2, shuffle=False)
    # for (idx, batch) in enumerate(DL):
    #     # Print the 'text' data of the batch
    #     print(idx, 'Construct ', batch['Construct'])
    #     # Print the 'class' data of batch
    #     print(idx, 'Label: ', batch['Label'], '\n')

    return TD

def main():
    # training_set = createDataset('data/sample_data_lessons_small.csv')
    # training_set = createDataset('data/Task_3_dataset/checkins_lessons_checkouts_training.csv')
    training_set = createDataset('data/Task_3_dataset/tmp_training.csv')
    print("Number of constructs: ", training_set.n_constructs)
    dkt = PermutedDKT(n_constructs=training_set.n_constructs).to(device)
    training_loader = DataLoader(training_set, batch_size=2, shuffle=False)
    learning_rate = 0.5
    optimizer = torch.optim.Adam(dkt.parameters(), lr=learning_rate)
    n_epochs = 50
    for epoch in range(n_epochs): # loop over the dataset multiple times
        train_loss=[]
        train_accuracy=[]
        for i, data in enumerate(training_loader, 0):
            print("Batch #: ", i)
            constructs = data['Construct']
            labels = data['Label']
            optimizer.zero_grad()
            print("constructs: ", torch.stack(constructs))
            print("labels: ", torch.stack(labels))
            loss, acc = dkt(torch.stack(constructs).to(device), torch.stack(labels).to(device))
            train_accuracy.append(acc)
            train_loss.append(loss.item())
            loss.backward()
            optimizer.step()
            print("--------"*10)
            print("loss: ", loss.data, "\nacc: ", acc.data)
        if (epoch + 1) % 5 == 0:
            print("========"*10)
            print(f"Epoch {epoch+1}/{n_epochs}, Train Loss: {loss.item():.4f}, Train Accuracy: {sum(train_accuracy)/len(train_accuracy):.2f}")
            print("========"*10)

    # dkt = PermutedDKT(n_constructs=5)
    # construct_input = torch.randint(0, 5, (4, 2))
    # labels = torch.randint(0, 2, (4, 2)) * 2 - 1
    # loss, acc = dkt(construct_input, labels)
    # print(loss, acc)


if __name__ == "__main__":
    main()