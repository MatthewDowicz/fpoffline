import numpy as np
import torch
from torch import nn
import pathlib

from fpoffline.denoise_utils.torch_model import DnCNN_B
from fpoffline.denoise_utils.numpy_model import np_DnCNN

device = "cuda" if torch.cuda.is_available() else "cpu"

def torch_grid_window(dataset,
                model,
                model_params,
                h_start,
                h_end,
                w_start,
                w_end,
                padding,
                filepath):
    
    """
    Pytorch implementation of the forward pass, ie. inference, of Denoising CNN
    (https://arxiv.org/pdf/1608.03981.pdf). This function denoises a specific 
    sized image patch of the larger noisy FVC image.
    
        
    Parameters:
    -----------
    dataset: `np.array`
        Noisy FVC image input.
    model: Pytorch model
        DnCNN_B
    model_params: `str`
        Models parameters for the trained model.
    h_start: `int`
        The height starting index of the inference window.
    h_end: `int`
        The height ending index of the inference window.
    w_start: `int`
        The horizontal starting index of the inference window.
    w_end: `int`
        The horizontal ending index of the inference window.
    padding: `int`
        How much to pad the FVC 6kx6k image. This is so we can 
        take larger patch sizes and thus have overlapping patchs
        that reduces the creation of artifacts within the denoised
        image.
    filepath: `str`
        Path that points to the directory that holds the weights file.
   
        
    Returns:
    --------
    resid_img: np.array
        Denoised image patch.
    """
      
    # Get the noisy image data 
    noise_data = dataset
    
    # Pad the full image, so that larger patch sizes are able to be used. This
    # allows for overlapping regions over the FVC image, which eliminates 
    # artifacts from cropping up.
    # NOTE: Padding is only applied to the (H,W) dimensions.
    noise_data = np.pad(noise_data, ((0,0), (0, 0), (padding, padding), (padding, padding)))
    
    # Get correct path to the model weights on shared directory.
    PATH = pathlib.Path(str(filepath))
    model_path = PATH / model_params
    assert model_path.exists()
    
    # Instantiate the model, put it onto the GPU, load the weights
    # of the trained model, and then set the model into evaluation mode
    # for inference.
    model = model()
    model.to(device)
    model.load_state_dict(torch.load(str(model_path)))
    model.eval()

    # Turn off gradient tracking so as to not update the model weights.
    with torch.no_grad():
                
        # Delete any remaining memory, turn the sub_image patch numpy array
        # into a torch tensor, so as to be compatible with the model, and
        # then put the data onto the GPU so as to be in the same place as 
        # the model.
        torch.cuda.empty_cache()
        test_noise = torch.as_tensor(noise_data[:, :, h_start:h_end, w_start:w_end])
        test_noise = test_noise.to(device)

        # Run the model on the noisy images, then detach the output from
        # the GPU and put it onto CPU while making it into a numpy array.
        # Delete 'output' so as to save memory
        output = model(test_noise)
        resid_img = output.detach().cpu().numpy()
        del output
        
        # Detach the noisy input image from GPU and put it onto CPU,
        # delete any memory of the input image off of the GPU, and then
        # for extra measure delete the variable to save memory.
        test_noise.detach().cpu()
        torch.cuda.empty_cache()
        del test_noise
        
        # One final check to delete everything to save memory.
        torch.cuda.empty_cache()
        
    return resid_img


def denoise_torch(data,
                 model=DnCNN_B,
                 model_params='2k_model_bs64_e800_ps50_Adam.pth',
                 filepath='/global/cfs/cdirs/desi/engineering/focalplane/endofnight/denoise/',
                 patch_size=2000,
                 padding=10):
    """
    Function that uses a sliding window method to run inference over the full
    FVC image and stitches the different unique windows back together for a 
    fully denoised FVC image.    
    
    Parameters:
    -----------
    data: np.array
        Array of the noisy FVC exposure.
    model: DnCNN_B
        The denoising CNN model to be used.
    model_params: str
        File name of the pickled OrderedDict of the trained model weights.
    filepath: str
        Path that points to where the models weights are stored.
    patch_size: int
        Size of the inference window.
        Defaults to 2000.
    padding: int
        How much to pad the input image to allow for artifact free 
        stitching of denoised patchs.
        Defaults to 10.
        
    Returns:
    --------
    full[0][0]: np.array
        Denoised FVC image that only returns (H,W) pixels NOT (N,C,H,W),
        which is the reason for the cuts in the first two dimensions.
    """
    
    # Reshape the image to be in the correct format to be used in model.
    noisy = np.reshape(data, (1, 1, 6000, 6000))
    
    # Get how many patchs fit within our FVC image.
    # E.g. patchs_per_dim == 3 if patch_size == 2000, which means 3 patchs fit
    # within the height and width of our FVC image.
    patchs_per_dim = int(len(data) / patch_size)
    
    # Create the indices of where one patch ends and the other one begins.
    # Save those together in a list for later use. Expected output will
    # be [0, 2000, 4000, 6000] if using patch_size=2000.
    window_end_idx = []
    for k in range(patchs_per_dim):
        window_end_idx.append(patch_size*(k))
    window_end_idx.append(len(data)) # appends endpt. ie. 6k
    
    # Instantiate an array of 0's of shape 6000x6000 for saving the denoised
    # inference patches in the correct location within the full 6k by 6k image.
    full = np.zeros((1, 1, 6000, 6000))

    # Loop that runs inference on unique regions of the full 6000x6000 image. 
    # The loop uses the patch indices created above to create the unique 
    # denoised patchs. For a 2k by 2k patch we'd run this 9 times to cover the
    # entire FVC image.
    for j in range(len(window_end_idx)-1):
        for i in range(len(window_end_idx)-1):
            
            denoised_patch = torch_grid_window(dataset=noisy,
                                        model=model,
                                        model_params=str(model_params),
                                        h_start=window_end_idx[i],
                                        h_end=window_end_idx[i+1]+(padding*2), 
                                        w_start=window_end_idx[j],
                                        w_end=window_end_idx[j+1]+(padding*2),
                                        padding=padding,
                                        filepath=filepath)
                                        # the reason for (padding*2) is b/c
                                        # padding is for only one side of
                                        # the img, but we need to pad both,
                                        # so the *2 accounts for this
            
            # Cut the padding we started with, so as to just have the FVC 
            # pixels.
            denoised_patch = denoised_patch[:, :, 10:-10, 10:-10]
            
            # Save the pixels in the correct location within the image.
            full[:, :, window_end_idx[i]:window_end_idx[i+1],
                 window_end_idx[j]:window_end_idx[j+1]] += denoised_patch

    # Save just the H,W dimensions of the denoised FVC image. Don't need the
    # sample/channel dimensions
    return full[0][0]



######### Numpy Model ######### 



def denoise_numpy(data,
                 model=np_DnCNN,
                 weights_dict='2k_model_bs64_e800_ps50_Adam.pth',
                 layer_list='2k_NP_layers_list.pkl',
                 im2col_mat='im2col_2k_indices.pkl',
                 filepath='/global/cfs/cdirs/desi/engineering/focalplane/endofnight/denoise/',
                 patch_size=2000,
                 padding=10):
    
    """
    Numpy FVC denoiser. 
    
    It runs via a sliding window method, where it denioses patchs of ~2kx2k 
    pixels. Doing it this way cuts down on memory and speeds up inference.
    
    
    Parameters:
    -----------
    data: np.array
        Input FVC image.
    model: Numpy model
        np_DnCNN
    weights_dict: Dict
        Dictionary of the weights for a 2k np_DnCNN model.
        Default is '2k_model_bs64_e800_ps50_Adam.pth'
    layer_list: str
        Name of the file that holds the names for each layer. Used in calling
        correct weights (for conv & bn layers) & index matrices to conduct 
        the `im2col` algorithm to quickly compute convolution layers.
        Default is '2k_NP_layers_list.pkl'.
    im2col_mat: str
        Name of the file that contains the index arrays for the numpy conv
        layers. These are needed for the `im2col` algorithm that computes
        the convolutions in an efficient and fast way.
        Default is 'im2col_2k_indices.pkl'.
    filepath: str
        Path to the directory housing the weights & indice arrays for the 
        numpy model.
        Default is '/global/cfs/cdirs/desi/engineering/focalplane/endofnight/denoise/.
    patch_size: int
        Width/height of the inference window that moves over the full 
        6k by 6k image. 
        Defaults to 2000.
    padding: int
        How much to pad the FVC image and patch image. Padding it allows
        for slight overlap between denoised patchs. This allows for no
        artifacts to come about, thus having a pure denoised image.
        Default is 10.
        
    Returns:
    --------
    full: np.array
        Full denoised FVC image.
    """
    
    # Load the saved im2col matrices of indices & im2col layer lists  
    # Create the path to the directory that houses the files.
    PATH = pathlib.Path(filepath)
    im2col_mat_path = PATH / im2col_mat
    assert im2col_mat_path.exists()
    layer_list_path = PATH / layer_list
    assert layer_list_path.exists()
    
    # Load the files from their specific paths
    loaded_im2col_mat = np.load(im2col_mat_path, allow_pickle=True)
    loaded_layer_list = np.load(layer_list_path, allow_pickle=True)
    
    # Get correct path to the model weights on shared diredctory
    # This is an interim shared directory & will update once David sends me
    # the new path that will be used in fpoffline.
    model_path = PATH / weights_dict
    assert model_path.exists()
    weights = torch.load(str(model_path))
    

    # Reshape the image to be in the correct format to be used in the model.  
    noisy = np.reshape(data, (1, 1, 6000, 6000))
    # Pad the FVC image for overlapping patchs
    # Doing this eliminates artifacts that show up at the edges of the
    # individual patchs.
    noisy = np.pad(noisy, ((0,0), (0, 0), (padding, padding), (padding, padding)))

    # Get how many patchs fit within our FVC image.
    # E.g. patchs_per_dim == 3 if patch_size == 2000
    # We're indexing on the H axis of the img ie. data[0][0] == 6000
    patchs_per_dim = int(len(data) / patch_size)
    
    # Create the indices of where one patch ends and the other one begins.
    # Save those together in a list for later use. Expected output will
    # be [0, 2000, 4000, 6000] if using patch_size=2000.
    window_end_idx = []
    for k in range(patchs_per_dim):
        window_end_idx.append(patch_size*(k))
    window_end_idx.append(len(data)) # appends endpt ie. 6k
    
    # Full image pass
    full = np.zeros((1, 1, 6000, 6000))

    for j in range(len(window_end_idx)-1):
        for i in range(len(window_end_idx)-1):

            # This gets the 2020x2020 patch we want to run denoise.
            # Adding the padding gets the H/W to be 2020 instead of 2000
            noise_data = noisy[:, :, 
                               window_end_idx[i]:window_end_idx[i+1]+(padding*2),
                               window_end_idx[j]:window_end_idx[j+1]+(padding*2)]
            
            # Run the model on this patch of the FVC image
            denoised_patch =  model(input_data=noise_data, 
                                       weights_dict=weights,
                                       layer_list=loaded_layer_list,
                                       im2col_mat=loaded_im2col_mat)
            
            # Crop the 10x10 border of the denoised_patch, so that when
            # we stitch it together there's no artifacts between neighboring
            # patchs.
            denoised_patch = denoised_patch[:, :, 10:-10, 10:-10]
            
            # Add the denoised patch to the empty array of FVC size
            full[:, :, window_end_idx[i]:window_end_idx[i+1],
                 window_end_idx[j]:window_end_idx[j+1]] += denoised_patch
            
    # Save just the H,W dimensions of the denoised FVC image. Don't need the
    # sample/channel dimensions
    return full[0][0]

