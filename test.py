import torch
import torch.nn as nn
import math

# torch.manual_seed(37)
# hidden_size = 3
# w = torch.empty(hidden_size, hidden_size)
# nn.init.kaiming_uniform_(w, a=math.sqrt(5))

# construct_input = torch.randint(0, 5, (4, 2))
# print(construct_input)

# print(construct_input.size(0))

a = torch.tensor([[1.0,2.0],[4.0,5.0]])
max_row = torch.max(a, dim=1).values.reshape(2, 1)
print(max_row)
ones = torch.ones(2).reshape(1, 2)
print(torch.matmul(max_row, ones))

