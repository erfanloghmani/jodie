'''
This code evaluates the validation and test performance in an epoch of the model trained in jodie.py.
The task is: interaction prediction, i.e., predicting which item will a user interact with?

To calculate the performance for one epoch:
$ python evaluate_interaction_prediction.py --network reddit --model jodie --epoch 49

To calculate the performance for all epochs, use the bash file, evaluate_all_epochs.sh, which calls this file once for every epoch.

Paper: Predicting Dynamic Embedding Trajectory in Temporal Interaction Networks. S. Kumar, X. Zhang, J. Leskovec. ACM SIGKDD International Conference on Knowledge Discovery and Data Mining (KDD), 2019.
'''

from library_data import *
from library_models import *
import json
from sklearn.cluster import KMeans

# INITIALIZE PARAMETERS
parser = argparse.ArgumentParser()
parser.add_argument('--network', required=True, help='Network name')
parser.add_argument('--model', default='jodie', help="Model name")
parser.add_argument('--gpu', default=-1, type=int, help='ID of the gpu to run on. If set to -1 (default), the GPU with most free memory will be chosen.')
parser.add_argument('--epoch', default=50, type=int, help='Epoch id to load')
parser.add_argument('--embedding_dim', default=128, type=int, help='Number of dimensions')
parser.add_argument('--train_proportion', default=0.8, type=float, help='Proportion of training interactions')
parser.add_argument('--state_change', default=True, type=bool, help='True if training with state change of users in addition to the next interaction prediction. False otherwise. By default, set to True. MUST BE THE SAME AS THE ONE USED IN TRAINING.')
args = parser.parse_args()
args.datapath = "data/%s.csv" % args.network
if args.train_proportion > 0.8:
    sys.exit('Training sequence proportion cannot be greater than 0.8.')
if args.network == "mooc":
    print "No interaction prediction for %s" % args.network
    sys.exit(0)

# SET GPU
if args.gpu == -1:
    args.gpu = select_free_gpu()
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)

# CHECK IF THE OUTPUT OF THE EPOCH IS ALREADY PROCESSED. IF SO, MOVE ON.
output_fname = "results/interaction_prediction_%s_%s.txt" % (args.model, args.network)
if os.path.exists(output_fname):
    f = open(output_fname, "r")
    search_string = 'Test performance of epoch %d' % args.epoch
    for line in f:
        line = line.strip()
        if search_string in line:
            print "Output file already has results of epoch %d" % args.epoch
            sys.exit(0)
    f.close()

# LOAD NETWORK
[user2id, user_sequence_id, user_timediffs_sequence, user_previous_itemid_sequence,
 item2id, item_sequence_id, item_timediffs_sequence,
 timestamp_sequence,
 feature_sequence,
 y_true, item_word_embs] = load_network(args)
num_interactions = len(user_sequence_id)
num_features = len(feature_sequence[0])
num_users = len(user2id)
num_items = len(item2id) + 1
true_labels_ratio = len(y_true) / (sum(y_true) + 1)
print "*** Network statistics:\n  %d users\n  %d items\n  %d interactions\n  %d/%d true labels ***\n\n" % (num_users, num_items, num_interactions, sum(y_true), len(y_true))

# SET TRAIN, VALIDATION, AND TEST BOUNDARIES
train_end_idx = validation_start_idx = int(num_interactions * args.train_proportion)
test_start_idx = int(num_interactions * (args.train_proportion + 0.1))
test_end_idx = int(num_interactions * (args.train_proportion + 0.2))

# SET BATCHING TIMESPAN
'''
Timespan indicates how frequently the model is run and updated.
All interactions in one timespan are processed simultaneously.
Longer timespans mean more interactions are processed and the training time is reduced, however it requires more GPU memory.
At the end of each timespan, the model is updated as well. So, longer timespan means less frequent model updates.
'''
timespan = timestamp_sequence[-1] - timestamp_sequence[0]
tbatch_timespan = timespan / 500

# INITIALIZE MODEL PARAMETERS
model = JODIE(args, num_features, num_users, num_items, item_word_embs.shape[1]).cuda()
weight = torch.Tensor([1, true_labels_ratio]).cuda()
crossEntropyLoss = nn.CrossEntropyLoss(weight=weight)
MSELoss = nn.MSELoss()
MSELoss_no_reduce = nn.MSELoss(reduction='none')

N_CLUSTERS = 32
km = KMeans(n_clusters=N_CLUSTERS, random_state=0).fit(item_word_embs)
item_clusters = km.predict(item_word_embs)

# INITIALIZE MODEL
learning_rate = 1e-3
optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-5)

# LOAD THE MODEL
model, optimizer, user_embeddings_dystat, item_embeddings_dystat, user_embeddings_timeseries, item_embeddings_timeseries, train_end_idx_training = load_model(model, optimizer, args, args.epoch)
if train_end_idx != train_end_idx_training:
    sys.exit('Training proportion during training and testing are different. Aborting.')

# SET THE USER AND ITEM EMBEDDINGS TO THEIR STATE AT THE END OF THE TRAINING PERIOD
set_embeddings_training_end(user_embeddings_dystat, item_embeddings_dystat, user_embeddings_timeseries, item_embeddings_timeseries, user_sequence_id, item_sequence_id, train_end_idx)

# LOAD THE EMBEDDINGS: DYNAMIC AND STATIC
item_embeddings = item_embeddings_dystat[:, :args.embedding_dim]
item_embeddings = item_embeddings.clone()
item_embeddings_static = item_embeddings_dystat[:, args.embedding_dim:]
item_embeddings_static = item_embeddings_static.clone()

user_embeddings = user_embeddings_dystat[:, :args.embedding_dim]
user_embeddings = user_embeddings.clone()
user_embeddings_static = user_embeddings_dystat[:, args.embedding_dim:]
user_embeddings_static = user_embeddings_static.clone()

item_word_embs_torch = torch.tensor(item_word_embs, dtype=torch.float).cuda()
user_last_word_in_cluster = torch.zeros((num_users, N_CLUSTERS, item_word_embs.shape[1])).cuda()
user_saw_cluster = torch.zeros((num_users, N_CLUSTERS)).cuda()

# PERFORMANCE METRICS
validation_ranks = []
test_ranks = []

'''
Here we use the trained model to make predictions for the validation and testing interactions.
The model does a forward pass from the start of validation till the end of testing.
For each interaction, the trained model is used to predict the embedding of the item it will interact with.
This is used to calculate the rank of the true item the user actually interacts with.

After this prediction, the errors in the prediction are used to calculate the loss and update the model parameters.
This simulates the real-time feedback about the predictions that the model gets when deployed in-the-wild.
Please note that since each interaction in validation and test is only seen once during the forward pass, there is no data leakage.
'''
tbatch_start_time = None
loss = 0
# FORWARD PASS
with trange(train_end_idx) as progress_bar:
    for j in progress_bar:
        userid = user_sequence_id[j]
        itemid_previous = user_previous_itemid_sequence[j]
        user_last_word_in_cluster[userid, item_clusters[itemid_previous], :] = item_word_embs_torch[itemid_previous]
        user_saw_cluster[userid, item_clusters[itemid_previous]] = 1

item_word_embs_repeat = torch.tensor(item_word_embs.repeat(N_CLUSTERS, 0), dtype=torch.float).cuda()

print "*** Making interaction predictions by forward pass (no t-batching) ***"
with trange(train_end_idx, test_end_idx) as progress_bar:
    for j in progress_bar:
        progress_bar.set_description('%dth interaction for validation and testing' % j)

        # LOAD INTERACTION J
        userid = user_sequence_id[j]
        itemid = item_sequence_id[j]
        feature = feature_sequence[j]
        user_timediff = user_timediffs_sequence[j]
        item_timediff = item_timediffs_sequence[j]
        timestamp = timestamp_sequence[j]
        if not tbatch_start_time:
            tbatch_start_time = timestamp
        itemid_previous = user_previous_itemid_sequence[j]

        user_last_word_in_cluster[userid, item_clusters[itemid_previous], :] = item_word_embs_torch[itemid_previous]
        user_saw_cluster[userid, item_clusters[itemid_previous]] = 1

        # LOAD USER AND ITEM EMBEDDING
        user_embedding_input = user_embeddings[torch.cuda.LongTensor([userid])]
        user_embedding_static_input = user_embeddings_static[torch.cuda.LongTensor([userid])]
        item_embedding_input = item_embeddings[torch.cuda.LongTensor([itemid])]
        item_embedding_static_input = item_embeddings_static[torch.cuda.LongTensor([itemid])]
        feature_tensor = Variable(torch.Tensor(feature).cuda()).unsqueeze(0)
        user_timediffs_tensor = Variable(torch.Tensor([user_timediff]).cuda()).unsqueeze(0)
        item_timediffs_tensor = Variable(torch.Tensor([item_timediff]).cuda()).unsqueeze(0)
        item_embedding_previous = item_embeddings[torch.cuda.LongTensor([itemid_previous])]

        item_word_embs_input = item_word_embs_torch[torch.cuda.LongTensor([itemid]), :]
        item_word_embs_previous = item_word_embs_torch[torch.cuda.LongTensor([itemid_previous]), :]
        feature_tensor_full = torch.cat([feature_tensor, item_word_embs_input], dim=1)

        # PROJECT USER EMBEDDING
        user_projected_embedding = model.forward(user_embedding_input, item_embedding_previous, timediffs=user_timediffs_tensor, features=feature_tensor, select='project')
        user_item_embedding = torch.cat([user_projected_embedding, item_embedding_previous, item_word_embs_previous, item_embeddings_static[torch.cuda.LongTensor([itemid_previous])], user_embedding_static_input], dim=1)

        # PREDICT ITEM EMBEDDING
        predicted_item_embedding = model.predict_item_embedding(user_item_embedding)

        cur_user_last_word_in_cluster = user_last_word_in_cluster[torch.cuda.LongTensor([userid])]
        cur_user_saw_cluster = user_saw_cluster[torch.cuda.LongTensor([userid])]
        cur_full_user_repeat = torch.cat([user_embedding_input, user_embeddings_static[torch.cuda.LongTensor([userid]), :]], dim=1).unsqueeze(1).repeat((1, N_CLUSTERS, 1))
        predicted_weights = model.predict_weight(cur_full_user_repeat, cur_user_last_word_in_cluster)

        weight_dynamic = predicted_item_embedding[:, -1]

        # CALCULATE PREDICTION LOSS
        # print(weight_dynamic.shape, predicted_item_embedding[:, :args.embedding_dim].shape, item_embedding_input.detach().shape)
        loss += torch.sum(torch.exp(weight_dynamic) * MSELoss_no_reduce(predicted_item_embedding[:, :args.embedding_dim], item_embedding_input.detach()).sum(1))
        loss += torch.sum(MSELoss_no_reduce(cur_user_last_word_in_cluster, item_word_embs_input.unsqueeze(1).repeat((1, N_CLUSTERS, 1))).sum(2) * torch.exp(predicted_weights).squeeze(2) * cur_user_saw_cluster)
        # loss += torch.sum(MSELoss_no_reduce(predicted_item_embedding[:, args.embedding_dim:-1], item_embedding_static_input).sum(1))

        # CALCULATE DISTANCE OF PREDICTED ITEM EMBEDDING TO ALL ITEMS
        euclidean_distances_dyn = nn.PairwiseDistance()(predicted_item_embedding[:, :args.embedding_dim].repeat(num_items, 1), item_embeddings).squeeze(-1)
        euclidean_distances_words = nn.PairwiseDistance()(cur_user_last_word_in_cluster.view(-1, 32).repeat(num_items, 1), item_word_embs_repeat).squeeze(-1).view(-1, num_items, 32).transpose(2, 1)
        # euclidean_distances_static = nn.PairwiseDistance()(predicted_item_embedding[:, args.embedding_dim:-1].repeat(num_items, 1), item_embeddings_static).squeeze(-1)

        agg_distances = torch.exp(weight_dynamic) * torch.pow(euclidean_distances_dyn, 2) + (torch.exp(predicted_weights) * torch.pow(euclidean_distances_words, 2) * cur_user_saw_cluster.unsqueeze(2)).sum(1) # + torch.pow(euclidean_distances_static, 2)

        # CALCULATE RANK OF THE TRUE ITEM AMONG ALL ITEMS
        true_item_distance = agg_distances[0, itemid]
        euclidean_distances_smaller = (agg_distances < true_item_distance).data.cpu().numpy()
        true_item_rank = np.sum(euclidean_distances_smaller) + 1

        if j < test_start_idx:
            validation_ranks.append(true_item_rank)
        else:
            test_ranks.append(true_item_rank)

        # UPDATE USER AND ITEM EMBEDDING
        user_embedding_output = model.forward(user_embedding_input, item_embedding_input, timediffs=user_timediffs_tensor, features=feature_tensor_full, select='user_update')
        item_embedding_output = model.forward(user_embedding_input, item_embedding_input, timediffs=item_timediffs_tensor, features=feature_tensor_full, select='item_update')

        # SAVE EMBEDDINGS
        item_embeddings[itemid, :] = item_embedding_output.squeeze(0)
        user_embeddings[userid, :] = user_embedding_output.squeeze(0)
        user_embeddings_timeseries[j, :] = user_embedding_output.squeeze(0)
        item_embeddings_timeseries[j, :] = item_embedding_output.squeeze(0)

        # CALCULATE LOSS TO MAINTAIN TEMPORAL SMOOTHNESS
        loss += MSELoss(item_embedding_output, item_embedding_input.detach())
        loss += MSELoss(user_embedding_output, user_embedding_input.detach())

        # CALCULATE STATE CHANGE LOSS
        if args.state_change:
            loss += calculate_state_prediction_loss(model, [j], user_embeddings_timeseries, y_true, crossEntropyLoss)

        # UPDATE THE MODEL IN REAL-TIME USING ERRORS MADE IN THE PAST PREDICTION
        if timestamp - tbatch_start_time > tbatch_timespan:
            tbatch_start_time = timestamp
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

            # RESET LOSS FOR NEXT T-BATCH
            loss = 0
            item_embeddings.detach_()
            user_embeddings.detach_()
            item_embeddings_timeseries.detach_()
            user_embeddings_timeseries.detach_()


json.dump(validation_ranks, open('results/validation_ranks_%s_%s_%s.json' % (args.epoch, args.model, args.network), 'w'))
json.dump(test_ranks, open('results/test_ranks_%s_%s_%s.json' % (args.epoch, args.model, args.network), 'w'))
# CALCULATE THE PERFORMANCE METRICS
performance_dict = dict()
ranks = validation_ranks
mrr = np.mean([1.0 / r for r in ranks])
rec10 = sum(np.array(ranks) <= 10) * 1.0 / len(ranks)
performance_dict['validation'] = [mrr, rec10]

ranks = test_ranks
mrr = np.mean([1.0 / r for r in ranks])
rec10 = sum(np.array(ranks) <= 10) * 1.0 / len(ranks)
performance_dict['test'] = [mrr, rec10]

# PRINT AND SAVE THE PERFORMANCE METRICS
fw = open(output_fname, "a")
metrics = ['Mean Reciprocal Rank', 'Recall@10']

print '\n\n*** Validation performance of epoch %d ***' % args.epoch
fw.write('\n\n*** Validation performance of epoch %d ***\n' % args.epoch)
for i in xrange(len(metrics)):
    print(metrics[i] + ': ' + str(performance_dict['validation'][i]))
    fw.write("Validation: " + metrics[i] + ': ' + str(performance_dict['validation'][i]) + "\n")

print '\n\n*** Test performance of epoch %d ***' % args.epoch
fw.write('\n\n*** Test performance of epoch %d ***\n' % args.epoch)
for i in xrange(len(metrics)):
    print(metrics[i] + ': ' + str(performance_dict['test'][i]))
    fw.write("Test: " + metrics[i] + ': ' + str(performance_dict['test'][i]) + "\n")

fw.flush()
fw.close()
