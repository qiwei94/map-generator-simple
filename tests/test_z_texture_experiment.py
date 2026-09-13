"""Small invariance tests for the opt-in Z diagnostic, not production defaults."""
import numpy as np
from tools.experiment_z_texture import choose_roof, ground_texture


def test_roof_keeps_minimum_thickness_and_whole_support():
    bottom = np.array([0., .05, .1])
    roof, policy = choose_roof(bottom, bottom + .5, 'draped_thickness', .24)
    assert policy == 'flat_above_support'
    assert np.isclose(roof, .6)
    assert np.all(roof-bottom >= .5-1e-12)
    # A crop containing only the low end must still use this whole-block roof.
    assert roof > bottom[0]+.5


def test_steep_and_existing_flat_roof_are_not_overridden():
    bottom = np.array([0., .3])
    assert choose_roof(bottom, bottom+.5, 'draped_thickness', .24)[1] == 'steep_keep_drape'
    assert choose_roof(bottom, bottom+.5, 'flat_roof_above_highest_support', .24)[0] is None


def test_texture_is_bounded_masked_and_deterministic():
    x,y=np.meshgrid(np.arange(80)*.03,np.arange(80)*.03)
    mask=np.zeros((80,80),bool);mask[10:70,10:70]=True
    z=ground_texture(x,y,mask,.03)
    assert np.array_equal(z,ground_texture(x,y,mask,.03))
    assert np.all(z[~mask]==0)
    assert z.min()>=0 and z.max()<=.07
    assert z.max()>.03
    assert np.all(z[10]==0)  # fade begins at zero on the mask edge
    assert np.all(ground_texture(x,y,mask,.03,amplitude_mm=0)==0)


def test_texture_coordinates_not_crop_index_define_interior_pattern():
    x,y=np.meshgrid(np.arange(100)*.03,np.arange(100)*.03)
    z=ground_texture(x,y,np.ones(x.shape,bool),.03)
    crop=ground_texture(x[10:90,10:90],y[10:90,10:90],np.ones((80,80),bool),.03)
    assert np.allclose(z[25:75,25:75],crop[15:65,15:65])
